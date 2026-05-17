"""
Forward USDC HyperEVM → HyperCore (HL trading account).

Mechanism:
  CoreDepositWallet on HyperEVM at 0x6b9e773128f453f5c2c60935ee2de2cbc5390a24
  - approve(USDC, CoreDepositWallet, amount)
  - deposit(uint256 amount, uint32 destinationDex)
      destinationDex = 0           → PERPS account
      destinationDex = 4294967295  → SPOT account

For our agent: destinationDex = 0 (PERPS) so USDC becomes margin for ETH-PERP.

Pre-conditions:
  - HYPE on HyperEVM for gas (~$0.50 worth = 0.05 HYPE at ~$10)
  - USDC on HyperEVM EOA at 0xb88339CB...

Run:
  DRY_RUN=true  python -m src.hc_deposit_test   # preview
  DRY_RUN=false python -m src.hc_deposit_test   # execute
"""
from __future__ import annotations

import logging
import time

import requests
from eth_account import Account
from web3 import Web3

from . import config

log = logging.getLogger("hc_deposit_test")

# HyperEVM constants
HYPEREVM_RPC = "https://rpc.hyperliquid.xyz/evm"
HYPEREVM_CHAIN_ID = 999

USDC_HYPEREVM = Web3.to_checksum_address("0xb88339CB7199b77E23DB6E890353E22632Ba630f")
CORE_DEPOSIT_WALLET = Web3.to_checksum_address("0x6b9e773128f453f5c2c60935ee2de2cbc5390a24")

DEST_PERPS = 0
DEST_SPOT  = 4294967295

ERC20_ABI = [
    {"inputs": [{"name": "a", "type": "address"}], "name": "balanceOf",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

CORE_DEPOSIT_ABI = [
    {"inputs": [{"name": "amount",         "type": "uint256"},
                {"name": "destinationDex", "type": "uint32"}],
     "name": "deposit", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
]


def hl_spot_usdc(addr: str) -> float:
    r = requests.post(f"{config.HL.api_url}/info",
                       json={"type": "spotClearinghouseState", "user": addr},
                       timeout=10)
    balances = r.json().get("balances", []) if r.status_code == 200 else []
    return next((float(b["total"]) for b in balances
                  if b.get("coin") == "USDC"), 0.0)


def hl_perp_margin(addr: str) -> float:
    r = requests.post(f"{config.HL.api_url}/info",
                       json={"type": "clearinghouseState", "user": addr},
                       timeout=10)
    return float(r.json().get("withdrawable", "0") or 0)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    pk = config.HL.private_key
    if not pk:
        log.error("No private key configured")
        return
    account = Account.from_key(pk)
    addr = account.address

    w3 = Web3(Web3.HTTPProvider(HYPEREVM_RPC))
    if not w3.is_connected():
        log.error(f"Cannot connect to HyperEVM RPC")
        return

    log.info("=" * 60)
    log.info("HyperEVM → HyperCore deposit test")
    log.info(f"  agent: {addr}")
    log.info(f"  DRY_RUN: {config.RISK.dry_run}")
    log.info("=" * 60)

    usdc = w3.eth.contract(address=USDC_HYPEREVM, abi=ERC20_ABI)
    cdw  = w3.eth.contract(address=CORE_DEPOSIT_WALLET, abi=CORE_DEPOSIT_ABI)

    # ─── Pre-flight ────────────────────────────────────────────
    hype_bal = w3.eth.get_balance(addr) / 1e18
    usdc_bal = usdc.functions.balanceOf(addr).call()
    spot_usdc = hl_spot_usdc(addr)
    perp_usdc = hl_perp_margin(addr)

    log.info("\n--- Pre-flight ---")
    log.info(f"  HyperEVM HYPE: {hype_bal:.6f}")
    log.info(f"  HyperEVM USDC: ${usdc_bal/1e6:.4f}")
    log.info(f"  HL SPOT USDC:  ${spot_usdc:.4f}")
    log.info(f"  HL PERP USDC:  ${perp_usdc:.4f}")

    if hype_bal < 0.001:
        log.error("FAIL: insufficient HYPE for gas (need ≥ 0.001)")
        return
    if usdc_bal < 100_000:
        log.error("FAIL: insufficient USDC on HyperEVM (need ≥ $0.10)")
        return

    # Use most of our USDC ($0.99) leaving small reserve
    amount = usdc_bal - 1   # leave 1 raw unit (negligible)
    log.info(f"\n--- Plan: deposit ${amount/1e6:.4f} USDC → PERPS (destDex=0) ---")

    if config.RISK.dry_run:
        log.info("[DRY_RUN] No tx submitted")
        return

    # Helper to send tx on HyperEVM
    nonce = w3.eth.get_transaction_count(addr)

    def send(fn, gas_limit: int = 200_000):
        nonlocal nonce
        tx = fn.build_transaction({
            "from":    addr,
            "nonce":   nonce,
            "chainId": HYPEREVM_CHAIN_ID,
            "gas":     gas_limit,
            "maxFeePerGas":         w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": int(0.001e9),
        })
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        nonce += 1
        log.info(f"  submitted: {h.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        log.info(f"    confirmed: status={receipt.status}, gas={receipt.gasUsed}")
        return receipt

    # ─── 1. Approve USDC → CoreDepositWallet ───────────────────
    log.info("\n--- Step 1: USDC.approve(CoreDepositWallet, max) ---")
    cur = usdc.functions.allowance(addr, CORE_DEPOSIT_WALLET).call()
    if cur >= amount:
        log.info(f"  already approved (allowance={cur})")
    else:
        send(usdc.functions.approve(CORE_DEPOSIT_WALLET, 2**256 - 1),
             gas_limit=100_000)
        time.sleep(2)  # propagation

    # ─── 2. CoreDepositWallet.deposit(amount, PERPS) ───────────
    log.info(f"\n--- Step 2: CoreDepositWallet.deposit({amount}, destDex=PERPS) ---")
    send(cdw.functions.deposit(amount, DEST_PERPS), gas_limit=200_000)

    # ─── 3. Verify ─────────────────────────────────────────────
    log.info("\n--- Polling for HC credit (60s timeout) ---")
    start = time.time()
    while time.time() - start < 60:
        post_perp = hl_perp_margin(addr)
        post_spot = hl_spot_usdc(addr)
        post_evm  = usdc.functions.balanceOf(addr).call() / 1e6
        if post_perp > perp_usdc + 0.01 or post_spot > spot_usdc + 0.01:
            log.info(f"  ✓ CREDITED to HC: perp +${post_perp-perp_usdc:.4f}, "
                     f"spot +${post_spot-spot_usdc:.4f}")
            break
        time.sleep(5)
        log.info(f"    +{int(time.time()-start)}s waiting…")
    else:
        log.warning("  no HC credit detected within 60s")

    # Final snapshot
    log.info("\n--- Post-deposit snapshot ---")
    log.info(f"  HyperEVM HYPE: {w3.eth.get_balance(addr)/1e18:.6f}")
    log.info(f"  HyperEVM USDC: ${usdc.functions.balanceOf(addr).call()/1e6:.4f}")
    log.info(f"  HL SPOT USDC:  ${hl_spot_usdc(addr):.4f}")
    log.info(f"  HL PERP USDC:  ${hl_perp_margin(addr):.4f}")


if __name__ == "__main__":
    main()
