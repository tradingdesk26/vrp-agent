"""
Isolated $1 test: bridge USDC Base → HyperEVM via CCTP V2.

Goals:
  1. Verify CCTP V2 depositForBurn works for our agent on Base
  2. Verify Iris attestation API responds correctly
  3. Empirically determine where USDC lands (HyperEVM EOA vs HL spot)
     given mintRecipient = our agent EOA

Run:
  DRY_RUN=true  python -m src.bridge_test    # preview
  DRY_RUN=false python -m src.bridge_test    # actually send $1
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

import requests
from web3 import Web3

from . import config
from .on_chain.client import BaseClient, USDC_BASE
from .on_chain.cctp import (
    CCTPBridge, TOKEN_MESSENGER_V2, DOMAIN_BASE, DOMAIN_HYPEREVM, IRIS_API,
)

log = logging.getLogger("bridge_test")

# HyperEVM mainnet
HYPEREVM_RPC = "https://rpc.hyperliquid.xyz/evm"
HYPEREVM_CHAIN_ID = 999

# USDC on HyperEVM. From Circle CCTP V2 deployment list — verify against
# Circle docs at https://developers.circle.com/cctp/evm-smart-contracts
USDC_HYPEREVM = "0xb88339CB7199b77E23DB6E890353E22632Ba630f"   # placeholder; verify

ERC20_BAL_ABI = [
    {"inputs": [{"name": "a", "type": "address"}], "name": "balanceOf",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}],
     "stateMutability": "view", "type": "function"},
]


def hl_spot_usdc(addr: str) -> float:
    """Read HL spot USDC balance via Info API."""
    r = requests.post(
        f"{config.HL.api_url}/info",
        json={"type": "spotClearinghouseState", "user": addr},
        timeout=10,
    )
    balances = r.json().get("balances", []) if r.status_code == 200 else []
    return next((float(b["total"]) for b in balances
                  if b.get("coin") == "USDC"), 0.0)


def hl_perp_margin(addr: str) -> float:
    r = requests.post(
        f"{config.HL.api_url}/info",
        json={"type": "clearinghouseState", "user": addr},
        timeout=10,
    )
    return float(r.json().get("withdrawable", "0") or 0)


def hyperevm_usdc(addr: str) -> tuple[float, str]:
    """Read USDC balance on HyperEVM. Returns (balance, error_msg)."""
    try:
        w3 = Web3(Web3.HTTPProvider(HYPEREVM_RPC))
        if not w3.is_connected():
            return 0.0, "RPC not connected"
        c = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_HYPEREVM),
            abi=ERC20_BAL_ABI,
        )
        try:
            sym = c.functions.symbol().call()
            dec = c.functions.decimals().call()
            bal = c.functions.balanceOf(Web3.to_checksum_address(addr)).call()
            return bal / (10 ** dec), f"symbol={sym} decimals={dec}"
        except Exception as e:
            return 0.0, f"USDC contract read failed: {e}"
    except Exception as e:
        return 0.0, f"web3 error: {e}"


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log.info("=" * 60)
    log.info("Isolated bridge test: $1 USDC Base → HyperEVM")
    log.info(f"  DRY_RUN = {config.RISK.dry_run}")
    log.info("=" * 60)

    base = BaseClient()
    cctp = CCTPBridge(base)

    # ─── Snapshot BEFORE ───────────────────────────────────────
    log.info("\n--- Pre-bridge snapshot ---")
    pre_base = base.balance(USDC_BASE) / 1e6
    pre_spot = hl_spot_usdc(base.address)
    pre_perp = hl_perp_margin(base.address)
    pre_evm, evm_info = hyperevm_usdc(base.address)
    log.info(f"  Base USDC:       ${pre_base:.4f}")
    log.info(f"  HL spot USDC:    ${pre_spot:.4f}")
    log.info(f"  HL perp margin:  ${pre_perp:.4f}")
    log.info(f"  HyperEVM USDC:   ${pre_evm:.4f} ({evm_info})")

    # ─── Bridge $1 ─────────────────────────────────────────────
    log.info("\n--- Submitting CCTP V2 burn ---")
    amount = 1_000_000  # $1 in raw 6-dec
    result = cctp.deposit_to_hl(amount, fast=True, max_fee_bps=50)  # 0.5% = $0.005 max
    log.info(f"  result: {result.status}, tx: {result.burn_tx}")
    if result.error:
        log.error(f"  error: {result.error}")
        return
    if config.RISK.dry_run:
        log.info("\n[DRY_RUN] No tx submitted. Re-run with DRY_RUN=false.")
        return

    # ─── Wait for attestation ──────────────────────────────────
    log.info(f"\n--- Polling Iris attestation API ---")
    log.info(f"  URL: {IRIS_API}/{DOMAIN_BASE}?transactionHash={result.burn_tx}")
    attestation = cctp.wait_for_attestation(result.burn_tx, src_domain=DOMAIN_BASE,
                                              max_wait_sec=180, poll_interval_sec=5)
    if attestation is None:
        log.error("  attestation timeout — manual intervention required")
        return
    log.info(f"  attestation complete!")
    log.info(f"    eventNonce: {attestation.get('eventNonce')}")

    # ─── Snapshot AFTER (poll for ~60s to catch landing) ───────
    log.info("\n--- Polling for USDC landing (60s timeout) ---")
    start = time.time()
    while time.time() - start < 60:
        post_evm, _ = hyperevm_usdc(base.address)
        post_spot = hl_spot_usdc(base.address)
        post_perp = hl_perp_margin(base.address)

        delta_evm  = post_evm  - pre_evm
        delta_spot = post_spot - pre_spot
        delta_perp = post_perp - pre_perp

        if delta_evm > 0 or delta_spot > 0 or delta_perp > 0:
            log.info(f"  landed: +${delta_evm:.4f} HyperEVM EOA / "
                     f"+${delta_spot:.4f} HL spot / +${delta_perp:.4f} HL perp")
            break
        time.sleep(5)
        log.info(f"    +{int(time.time()-start)}s waiting...")
    else:
        log.warning("  no balance change detected within 60s")

    # ─── Final snapshot ────────────────────────────────────────
    log.info("\n--- Post-bridge snapshot ---")
    post_base = base.balance(USDC_BASE) / 1e6
    post_spot = hl_spot_usdc(base.address)
    post_perp = hl_perp_margin(base.address)
    post_evm, _ = hyperevm_usdc(base.address)
    log.info(f"  Base USDC:       ${post_base:.4f}  (delta {post_base-pre_base:+.4f})")
    log.info(f"  HL spot USDC:    ${post_spot:.4f}  (delta {post_spot-pre_spot:+.4f})")
    log.info(f"  HL perp margin:  ${post_perp:.4f}  (delta {post_perp-pre_perp:+.4f})")
    log.info(f"  HyperEVM USDC:   ${post_evm:.4f}  (delta {post_evm-pre_evm:+.4f})")

    log.info("\n" + "=" * 60)
    log.info("CONCLUSION:")
    if post_spot > pre_spot + 0.01:
        log.info("  ✓ USDC arrived on HL SPOT — auto-forward to HC works")
    elif post_perp > pre_perp + 0.01:
        log.info("  ✓ USDC arrived on HL PERP — auto-forward to HC works")
    elif post_evm > pre_evm + 0.01:
        log.info("  ⚠ USDC stuck on HyperEVM EOA — manual forward to HC needed")
        log.info("    Next step: research CoreDepositWallet contract on HyperEVM")
    else:
        log.info("  ✗ USDC NOT detected anywhere yet — may need more wait time")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
