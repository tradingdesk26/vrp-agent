"""
Phase 2 forward transition: PARKED_IN_LP → LONG_ON_HL.

Pipeline (sequential, all individually validated):
  1. POSM.burn(tokenId)              receive ETH + USDC on Base
  2. V3 swap ETH → USDC (Uni v3)     consolidate to USDC, keep gas reserve
  3. CCTP V2 USDC Base → HyperEVM    ~22s + Iris attestation
  4. CoreDepositWallet.deposit USDC  HyperEVM → HL spot (unified margin)
  5. HL exchange.market_open ETH     long ETH-PERP

Idempotency:
  Each step checks current state and skips if already done.
  Restart-safe: reading on-chain state determines what's left.

Run:
  DRY_RUN=true  python -m src.phase2_forward     # preview
  DRY_RUN=false python -m src.phase2_forward     # execute
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

import requests
from eth_account import Account
from web3 import Web3

from . import config
from .hl_executor import HLExecutor
from .on_chain.cctp import CCTPBridge, DOMAIN_BASE
from .on_chain.client import (
    BaseClient, USDC_BASE, NATIVE_ETH, HOOK_ETH_USDC,
    make_pool_key_eth_usdc,
)
from .on_chain.lp_manager import LPManager
from .on_chain.swap_v3 import V3Swap
from .pnl_tracker import PnLTracker

log = logging.getLogger("phase2_forward")

# HyperEVM constants
HYPEREVM_RPC = "https://rpc.hyperliquid.xyz/evm"
HYPEREVM_CHAIN_ID = 999
USDC_HYPEREVM = Web3.to_checksum_address("0xb88339CB7199b77E23DB6E890353E22632Ba630f")
CORE_DEPOSIT_WALLET = Web3.to_checksum_address("0x6b9e773128f453f5c2c60935ee2de2cbc5390a24")
DEST_PERPS = 0

# Reserve some ETH for future gas on Base
GAS_RESERVE_ETH_WEI = 1_000_000_000_000_000  # 0.001 ETH

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


def hyperevm_w3():
    return Web3(Web3.HTTPProvider(HYPEREVM_RPC))


def hyperevm_usdc_balance(addr: str) -> int:
    w3 = hyperevm_w3()
    c = w3.eth.contract(address=USDC_HYPEREVM, abi=ERC20_ABI)
    return c.functions.balanceOf(Web3.to_checksum_address(addr)).call()


def hl_spot_usdc(addr: str) -> float:
    r = requests.post(f"{config.HL.api_url}/info",
                       json={"type": "spotClearinghouseState", "user": addr},
                       timeout=10)
    balances = r.json().get("balances", []) if r.status_code == 200 else []
    return next((float(b["total"]) for b in balances
                  if b.get("coin") == "USDC"), 0.0)


def run_forward(entry_mode: str = "session", session_date: str | None = None,
                 entry_reason: str = "phase2_forward"):
    """PARKED_IN_LP → LONG_ON_HL.

    Args:
        entry_mode: 'session' or 'persistent' — persisted in trades.entry_mode
        session_date: 'YYYY-MM-DD' UTC, set only for session-mode entries
        entry_reason: text reason, persisted in trades.entry_reason
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log.info("=" * 60)
    log.info(f"Phase 2 FORWARD: PARKED_IN_LP → LONG_ON_HL ({entry_mode})")
    log.info(f"  DRY_RUN: {config.RISK.dry_run}")
    log.info("=" * 60)

    base = BaseClient()
    lp   = LPManager(base)
    swap = V3Swap(base)
    cctp = CCTPBridge(base)
    hl   = HLExecutor()
    tracker = PnLTracker()

    # ─── Step 0: Pre-flight ────────────────────────────────────
    active = tracker.active_lp()
    if not active:
        log.error("No active LP in SQLite. Aborting.")
        return
    token_id = active["token_id"]
    log.info(f"\n--- Pre-flight ---")
    log.info(f"  active LP tokenId: {token_id} ({active['pool_label']})")

    pos = lp.read_position(token_id)
    if not pos.exists or pos.liquidity == 0:
        log.error(f"LP tokenId {token_id} not active on-chain — aborting")
        return

    pre_eth  = base.w3.eth.get_balance(base.address)
    pre_usdc = base.balance(USDC_BASE)
    pre_evm_usdc = hyperevm_usdc_balance(base.address)
    pre_hl_spot  = hl_spot_usdc(base.address)
    log.info(f"  Base ETH:       {pre_eth/1e18:.6f}")
    log.info(f"  Base USDC:      ${pre_usdc/1e6:.4f}")
    log.info(f"  HyperEVM USDC:  ${pre_evm_usdc/1e6:.4f}")
    log.info(f"  HL spot USDC:   ${pre_hl_spot:.4f}")

    # ─── Step 1: Burn LP ───────────────────────────────────────
    log.info(f"\n--- Step 1: burn POSM NFT {token_id} ---")
    burn_res = lp.burn(token_id)
    log.info(f"  result: {burn_res.status}")
    if burn_res.tx_hash:
        log.info(f"  tx: {burn_res.tx_hash}")
    if burn_res.error:
        log.error(f"  error: {burn_res.error}")
        return
    if not config.RISK.dry_run:
        tracker.record_lp_burn(
            token_id=token_id,
            ts=datetime.utcnow().isoformat(),
            tx=burn_res.tx_hash or "",
        )
        time.sleep(2)
        log.info(f"  Base ETH after burn:  {base.w3.eth.get_balance(base.address)/1e18:.6f}")
        log.info(f"  Base USDC after burn: ${base.balance(USDC_BASE)/1e6:.4f}")

    # ─── Step 2: Swap ETH → USDC (keep gas reserve) ───────────
    if config.RISK.dry_run:
        # Estimate post-burn ETH/USDC for plan preview
        eth_freed = 1_000_000_000_000_000  # ~0.001 ETH from LP
        post_burn_eth = pre_eth + eth_freed
        eth_to_swap = max(0, post_burn_eth - GAS_RESERVE_ETH_WEI)
    else:
        post_burn_eth = base.w3.eth.get_balance(base.address)
        eth_to_swap = max(0, post_burn_eth - GAS_RESERVE_ETH_WEI)

    log.info(f"\n--- Step 2: swap {eth_to_swap/1e18:.6f} ETH → USDC ---")
    log.info(f"  (keeping {GAS_RESERVE_ETH_WEI/1e18:.4f} ETH gas reserve)")
    if eth_to_swap < 10_000_000_000_000:  # < 0.00001 ETH
        log.info("  too little ETH to swap, skipping")
    else:
        pre_swap_usdc = base.balance(USDC_BASE)
        swap_res = swap.eth_to_usdc(eth_to_swap)
        log.info(f"  result: {swap_res.status}")
        if swap_res.tx_hash:
            log.info(f"  tx: {swap_res.tx_hash}")
        if swap_res.error:
            log.error(f"  error: {swap_res.error}")
            return
        if not config.RISK.dry_run:
            # Poll until USDC balance reflects swap
            log.info(f"  polling USDC balance update…")
            poll_start = time.time()
            while time.time() - poll_start < 30:
                cur = base.balance(USDC_BASE)
                if cur >= pre_swap_usdc + 100_000:  # +$0.10 minimum
                    log.info(f"  USDC balance updated: ${cur/1e6:.4f} "
                             f"(+${(cur - pre_swap_usdc)/1e6:.4f})")
                    break
                time.sleep(1)
            else:
                log.warning("  USDC balance did not update within 30s")

    # ─── Step 3: CCTP USDC → HyperEVM ─────────────────────────
    if config.RISK.dry_run:
        bridge_amount = 5_000_000   # placeholder $5 for preview
    else:
        bridge_amount = base.balance(USDC_BASE) - 10_000  # leave $0.01 buffer

    log.info(f"\n--- Step 3: CCTP burn ${bridge_amount/1e6:.4f} USDC → HyperEVM ---")
    if bridge_amount < 1_000_000:
        log.error("  insufficient USDC after swap (< $1) — aborting")
        return
    bridge_res = cctp.deposit_to_hl(bridge_amount, fast=True, max_fee_bps=50)
    log.info(f"  result: {bridge_res.status}, burn_tx: {bridge_res.burn_tx}")
    if bridge_res.error:
        log.error(f"  error: {bridge_res.error}")
        return

    if config.RISK.dry_run:
        log.info("\n[DRY_RUN] Skipping Iris attestation + HC deposit + HL market_open")
        log.info(f"  Would: poll Iris → deposit({bridge_amount} → PERPS) → market_open ETH")
        return

    # ─── Step 4: Wait Iris attestation ─────────────────────────
    log.info(f"\n--- Step 4: Iris attestation (~30s) ---")
    attestation = cctp.wait_for_attestation(
        bridge_res.burn_tx, src_domain=DOMAIN_BASE,
        max_wait_sec=300, poll_interval_sec=5,
    )
    if not attestation:
        log.error("  attestation timeout")
        return
    log.info("  attestation complete")

    # ─── Step 5: Wait for USDC to mint on HyperEVM ─────────────
    # CCTP V2 Iris attestation typically completes in ~10s, but Circle's
    # auto-relayer to HyperEVM has been observed taking 60-120s. Use 180s
    # to absorb relayer slowness; if still nothing, drop to UNKNOWN and
    # let downstream recovery (state_machine IN_TRANSIT_TO_HL handling)
    # take care of it.
    log.info("\n--- Step 5: poll HyperEVM USDC arrival (180s) ---")
    start = time.time()
    while time.time() - start < 180:
        cur_evm = hyperevm_usdc_balance(base.address)
        if cur_evm >= bridge_amount * 0.99:  # allow small fee
            log.info(f"  HyperEVM USDC arrived: ${cur_evm/1e6:.4f}")
            break
        time.sleep(3)
    else:
        log.error("  USDC not detected on HyperEVM within 180s")
        return

    # ─── Step 6: CoreDepositWallet.deposit ─────────────────────
    log.info(f"\n--- Step 6: CoreDepositWallet.deposit → HC PERPS ---")
    w3evm = hyperevm_w3()
    account = Account.from_key(config.HL.private_key)
    usdc_c = w3evm.eth.contract(address=USDC_HYPEREVM, abi=ERC20_ABI)
    cdw = w3evm.eth.contract(address=CORE_DEPOSIT_WALLET, abi=CORE_DEPOSIT_ABI)

    evm_amount = usdc_c.functions.balanceOf(base.address).call()
    nonce = w3evm.eth.get_transaction_count(base.address)

    def send_evm(fn, gas_limit=200_000):
        nonlocal nonce
        tx = fn.build_transaction({
            "from":    base.address,
            "nonce":   nonce,
            "chainId": HYPEREVM_CHAIN_ID,
            "gas":     gas_limit,
            "maxFeePerGas":         w3evm.eth.gas_price * 2,
            "maxPriorityFeePerGas": int(0.001e9),
        })
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3evm.eth.send_raw_transaction(raw)
        nonce += 1
        log.info(f"    submitted: {h.hex()}")
        r = w3evm.eth.wait_for_transaction_receipt(h, timeout=120)
        log.info(f"    confirmed: status={r.status}, gas={r.gasUsed}")
        return r

    # 6a. approve
    cur_allow = usdc_c.functions.allowance(base.address, CORE_DEPOSIT_WALLET).call()
    if cur_allow < evm_amount:
        log.info("  approving USDC → CoreDepositWallet")
        send_evm(usdc_c.functions.approve(CORE_DEPOSIT_WALLET, 2**256 - 1), gas_limit=100_000)
        time.sleep(2)

    # 6b. deposit
    log.info(f"  deposit({evm_amount}, dest=PERPS)")
    send_evm(cdw.functions.deposit(evm_amount, DEST_PERPS), gas_limit=200_000)

    # 6c. Wait HL credit
    log.info("\n--- Step 7: poll HL credit (180s) ---")
    start = time.time()
    while time.time() - start < 180:
        cur_hl = hl_spot_usdc(base.address) + hl.get_usdc_balance()
        if cur_hl > pre_hl_spot + (bridge_amount / 1e6) * 0.99:
            log.info(f"  HL credit detected: total margin = ${cur_hl:.4f}")
            break
        time.sleep(3)
    else:
        log.warning("  HL credit not detected within 180s")

    # ─── Step 8: Open long ETH-PERP ────────────────────────────
    log.info(f"\n--- Step 8: open long ETH-PERP ---")
    size_usd = min(config.RISK.max_position_usd,
                    hl.get_usdc_balance() * 0.95)
    log.info(f"  size: ${size_usd:.2f}")
    if size_usd < 1.0:
        log.warning(f"  HL margin too low for trade")
        return
    open_res = hl.open_long(size_usd)
    log.info(f"  result: {open_res.get('status')}")
    log.info(f"  raw: {open_res}")

    # Record trade in SQLite
    tracker.open_trade(
        ts=datetime.utcnow().isoformat(),
        price=open_res.get("mid_price", 0.0),
        size_eth=open_res.get("size_eth", 0.0),
        reason=entry_reason,
        dry_run=False,
        raw=open_res,
        entry_mode=entry_mode,
        session_date=session_date,
    )

    log.info("\n" + "=" * 60)
    log.info(f"✓ Phase 2 FORWARD complete: LP → HL long position ({entry_mode})")
    log.info("=" * 60)


def run_forward_from_cash(entry_mode: str = "session", session_date: str | None = None,
                            entry_reason: str = "phase_session_from_defensive"):
    """DEFENSIVE_CASH (USDC on Base) → LONG_ON_HL.

    Skips Step 1 (burn LP) and Step 2 (swap ETH→USDC) of run_forward — capital
    is already as Base USDC. Runs Steps 3-8 (CCTP, deposit, open long).

    Used when defensive trigger fired earlier in the day and VRP did not
    recover above 0 by 20:00 UTC, but the session-timing edge still applies.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log.info("=" * 60)
    log.info(f"Phase FORWARD-FROM-CASH: DEFENSIVE_CASH → LONG_ON_HL ({entry_mode})")
    log.info(f"  DRY_RUN: {config.RISK.dry_run}")
    log.info("=" * 60)

    base = BaseClient()
    cctp = CCTPBridge(base)
    hl   = HLExecutor()
    tracker = PnLTracker()

    # ─── Pre-flight ────────────────────────────────────────────
    pre_eth      = base.w3.eth.get_balance(base.address)
    pre_usdc     = base.balance(USDC_BASE)
    pre_evm_usdc = hyperevm_usdc_balance(base.address)
    pre_hl_spot  = hl_spot_usdc(base.address)
    log.info(f"\n--- Pre-flight ---")
    log.info(f"  Base ETH:       {pre_eth/1e18:.6f}")
    log.info(f"  Base USDC:      ${pre_usdc/1e6:.4f}")
    log.info(f"  HyperEVM USDC:  ${pre_evm_usdc/1e6:.4f}")
    log.info(f"  HL spot USDC:   ${pre_hl_spot:.4f}")

    if pre_usdc < 1_000_000:
        log.error(f"  insufficient Base USDC (${pre_usdc/1e6:.2f} < $1) — aborting")
        return

    # ─── Step 3: CCTP USDC Base → HyperEVM ─────────────────────
    bridge_amount = pre_usdc - 10_000   # leave $0.01 buffer
    log.info(f"\n--- Step 3: CCTP burn ${bridge_amount/1e6:.4f} USDC → HyperEVM ---")
    bridge_res = cctp.deposit_to_hl(bridge_amount, fast=True, max_fee_bps=50)
    log.info(f"  result: {bridge_res.status}, burn_tx: {bridge_res.burn_tx}")
    if bridge_res.error:
        log.error(f"  error: {bridge_res.error}")
        return

    if config.RISK.dry_run:
        log.info("\n[DRY_RUN] Skipping Iris attestation + HC deposit + HL market_open")
        return

    # ─── Step 4: Wait Iris attestation ─────────────────────────
    log.info(f"\n--- Step 4: Iris attestation (~30s) ---")
    attestation = cctp.wait_for_attestation(
        bridge_res.burn_tx, src_domain=DOMAIN_BASE,
        max_wait_sec=300, poll_interval_sec=5,
    )
    if not attestation:
        log.error("  attestation timeout")
        return
    log.info("  attestation complete")

    # ─── Step 5: Wait for USDC to mint on HyperEVM ─────────────
    log.info("\n--- Step 5: poll HyperEVM USDC arrival (180s) ---")
    start = time.time()
    while time.time() - start < 180:
        cur_evm = hyperevm_usdc_balance(base.address)
        if cur_evm >= bridge_amount * 0.99:
            log.info(f"  HyperEVM USDC arrived: ${cur_evm/1e6:.4f}")
            break
        time.sleep(3)
    else:
        log.error("  USDC not detected on HyperEVM within 180s")
        return

    # ─── Step 6: CoreDepositWallet.deposit ─────────────────────
    log.info(f"\n--- Step 6: CoreDepositWallet.deposit → HC PERPS ---")
    w3evm = hyperevm_w3()
    account = Account.from_key(config.HL.private_key)
    usdc_c = w3evm.eth.contract(address=USDC_HYPEREVM, abi=ERC20_ABI)
    cdw = w3evm.eth.contract(address=CORE_DEPOSIT_WALLET, abi=CORE_DEPOSIT_ABI)

    evm_amount = usdc_c.functions.balanceOf(base.address).call()
    nonce = w3evm.eth.get_transaction_count(base.address)

    def send_evm(fn, gas_limit=200_000):
        nonlocal nonce
        tx = fn.build_transaction({
            "from":    base.address,
            "nonce":   nonce,
            "chainId": HYPEREVM_CHAIN_ID,
            "gas":     gas_limit,
            "maxFeePerGas":         w3evm.eth.gas_price * 2,
            "maxPriorityFeePerGas": int(0.001e9),
        })
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3evm.eth.send_raw_transaction(raw)
        nonce += 1
        log.info(f"    submitted: {h.hex()}")
        r = w3evm.eth.wait_for_transaction_receipt(h, timeout=120)
        log.info(f"    confirmed: status={r.status}, gas={r.gasUsed}")
        return r

    cur_allow = usdc_c.functions.allowance(base.address, CORE_DEPOSIT_WALLET).call()
    if cur_allow < evm_amount:
        log.info("  approving USDC → CoreDepositWallet")
        send_evm(usdc_c.functions.approve(CORE_DEPOSIT_WALLET, 2**256 - 1), gas_limit=100_000)
        time.sleep(2)

    log.info(f"  deposit({evm_amount}, dest=PERPS)")
    send_evm(cdw.functions.deposit(evm_amount, DEST_PERPS), gas_limit=200_000)

    # ─── Step 7: poll HL credit ────────────────────────────────
    log.info("\n--- Step 7: poll HL credit (60s) ---")
    start = time.time()
    while time.time() - start < 60:
        cur_hl = hl_spot_usdc(base.address) + hl.get_usdc_balance()
        if cur_hl > pre_hl_spot + (bridge_amount / 1e6) * 0.99:
            log.info(f"  HL credit detected: total margin = ${cur_hl:.4f}")
            break
        time.sleep(3)
    else:
        log.warning("  HL credit not detected within 60s")

    # ─── Step 8: Open long ETH-PERP ────────────────────────────
    log.info(f"\n--- Step 8: open long ETH-PERP ---")
    size_usd = min(config.RISK.max_position_usd,
                    hl.get_usdc_balance() * 0.95)
    log.info(f"  size: ${size_usd:.2f}")
    if size_usd < 1.0:
        log.warning(f"  HL margin too low for trade")
        return
    open_res = hl.open_long(size_usd)
    log.info(f"  result: {open_res.get('status')}")

    tracker.open_trade(
        ts=datetime.utcnow().isoformat(),
        price=open_res.get("mid_price", 0.0),
        size_eth=open_res.get("size_eth", 0.0),
        reason=entry_reason,
        dry_run=False,
        raw=open_res,
        entry_mode=entry_mode,
        session_date=session_date,
    )

    log.info("\n" + "=" * 60)
    log.info(f"✓ Phase FORWARD-FROM-CASH complete: DEFENSIVE_CASH → LONG ({entry_mode})")
    log.info("=" * 60)


if __name__ == "__main__":
    run_forward()
