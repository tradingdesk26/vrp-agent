"""
Bootstrap: park agent capital in ETH/USDC pool (round-30 hook, big TVL).

Strategy: no swap needed — agent has both ETH (for gas) and USDC. Use a
small fraction of each to mint LP. Pool is large enough that any price
impact would be ~0.0002%.

For first live test:
  ETH side: 0.001 ETH (~$2.29 at $2288/ETH)
  USDC side: $2.29 (matching value)
  → LP value ~$4.58 (small, validates mechanism)

Approvals: only USDC needs Permit2 (ETH is native).

Run:
  python -m src.bootstrap_pool2            # dry-run preview
  DRY_RUN=false python -m src.bootstrap_pool2   # actually execute
"""
from __future__ import annotations

import logging

from datetime import datetime

from . import config
from .on_chain.client import (
    BaseClient, USDC_BASE, NATIVE_ETH, HOOK_ETH_USDC, POSM,
    make_pool_key_eth_usdc, compute_pool_id, read_pool_slot0,
)
from .on_chain.lp_manager import LPManager
from .on_chain.liquidity_math import (
    liquidity_from_amounts, tick_to_sqrt_price_x96,
    amount0_from_liquidity, amount1_from_liquidity,
)
from .pnl_tracker import PnLTracker

POSM_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


def _extract_token_id(receipt, recipient_addr: str) -> int | None:
    """Find ERC721 Transfer log on POSM with to=recipient → return tokenId."""
    for log in receipt.logs:
        if log.address.lower() != POSM.lower():
            continue
        if len(log.topics) < 4:
            continue
        if log.topics[0].hex().lower() not in (
            POSM_TRANSFER_TOPIC, POSM_TRANSFER_TOPIC[2:]
        ):
            continue
        to_addr = "0x" + log.topics[2].hex()[-40:]
        if to_addr.lower() == recipient_addr.lower():
            return int(log.topics[3].hex(), 16)
    return None

log = logging.getLogger("bootstrap2")

# Target ETH amount (small for first test). Pool is big — no impact concerns.
TARGET_ETH_WEI = 1_000_000_000_000_000   # 0.001 ETH = ~$2.29
GAS_RESERVE_ETH_WEI = 1_500_000_000_000_000  # keep 0.0015 ETH for gas
SAFETY_BUFFER_BPS = 100  # 1% slippage tolerance

TICK_LOWER = -887220
TICK_UPPER =  887220


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log.info("=" * 60)
    log.info("Bootstrap: park into ETH/USDC pool (round-30)")
    log.info(f"  TARGET_ETH = {TARGET_ETH_WEI / 1e18:.6f} ETH")
    log.info(f"  DRY_RUN    = {config.RISK.dry_run}")
    log.info("=" * 60)

    base = BaseClient()
    lp = LPManager(base)

    # ─── 1. Pre-flight ─────────────────────────────────────────
    usdc_bal = base.balance(USDC_BASE)
    eth_bal  = base.w3.eth.get_balance(base.address)
    log.info("\n--- Pre-flight ---")
    log.info(f"  USDC: {usdc_bal/1e6:.4f}")
    log.info(f"  ETH:  {eth_bal/1e18:.6f}")

    if eth_bal < TARGET_ETH_WEI + GAS_RESERVE_ETH_WEI:
        log.error(f"FAIL: need {(TARGET_ETH_WEI + GAS_RESERVE_ETH_WEI)/1e18:.6f} ETH "
                   f"(target {TARGET_ETH_WEI/1e18} + gas reserve {GAS_RESERVE_ETH_WEI/1e18}), "
                   f"have {eth_bal/1e18:.6f}")
        return

    # ─── 2. Read pool state ────────────────────────────────────
    pk = make_pool_key_eth_usdc()
    pool_id = compute_pool_id(pk)
    slot0 = read_pool_slot0(base, pool_id)
    sqrt_p = slot0["sqrt_price_x96"]
    cur_tick = slot0["tick"]
    Q96 = 1 << 96
    raw_price = (sqrt_p / Q96) ** 2
    human_price = raw_price * 10**12   # ETH18 dec, USDC 6 dec → ×10^12
    log.info("\n--- Pool state ---")
    log.info(f"  poolId: 0x{pool_id.hex()}")
    log.info(f"  sqrtPriceX96: {sqrt_p}")
    log.info(f"  tick: {cur_tick}")
    log.info(f"  price (USDC per ETH): {human_price:.2f}")

    # ─── 3. Compute amounts ────────────────────────────────────
    # For full-range LP, the binding amount ratio at current price:
    #   amount1_USDC / amount0_ETH = P (= USDC per ETH for raw units after dec adjustment)
    # We provide TARGET_ETH; compute matching USDC.
    sqrt_pl = tick_to_sqrt_price_x96(TICK_LOWER)
    sqrt_pu = tick_to_sqrt_price_x96(TICK_UPPER)

    # We have a fixed ETH amount; compute the L it produces and the matching USDC needed
    # Use upper bound USDC = TARGET_ETH * P at human level, then convert raw
    eth_in_usd = TARGET_ETH_WEI / 1e18 * human_price
    target_usdc = int(eth_in_usd * 1e6)   # raw 6-dec
    log.info(f"\n  matching USDC for {TARGET_ETH_WEI/1e18:.6f} ETH: ${eth_in_usd:.4f}")

    if usdc_bal < target_usdc + 100_000:  # need at least $0.10 buffer
        log.error(f"FAIL: need {target_usdc/1e6:.4f} USDC, have {usdc_bal/1e6:.4f}")
        return

    # Now compute L precisely
    L = liquidity_from_amounts(sqrt_p, sqrt_pl, sqrt_pu, TARGET_ETH_WEI, target_usdc)
    a0 = amount0_from_liquidity(sqrt_p, sqrt_pu, L)  # ETH wei
    a1 = amount1_from_liquidity(sqrt_pl, sqrt_p, L)  # USDC raw
    a0_max = a0 + a0 * SAFETY_BUFFER_BPS // 10_000
    a1_max = a1 + a1 * SAFETY_BUFFER_BPS // 10_000
    log.info(f"\n--- L computation ---")
    log.info(f"  L: {L:,}")
    log.info(f"  amount0 (ETH wei):  {a0:,} (max {a0_max:,})  = {a0/1e18:.6f} ETH")
    log.info(f"  amount1 (USDC raw): {a1:,} (max {a1_max:,})  = ${a1/1e6:.4f}")

    # ─── 4. Approve USDC → Permit2 → POSM ──────────────────────
    log.info("\n--- Approvals (USDC only — ETH is native, no approval needed) ---")
    approval_results = lp.setup_approvals([USDC_BASE])
    failed = [r for r in approval_results if r.status == "error"]
    if failed:
        log.error(f"FAIL: approval step errored: {[r.error for r in failed]}")
        return

    # ─── 5. Mint ───────────────────────────────────────────────
    log.info("\n--- Mint LP ---")
    log.info(f"  msg.value (ETH for currency0): {a0_max} wei = {a0_max/1e18:.6f} ETH")
    mint_res = lp.mint(
        pool_key=pk,
        tick_lower=TICK_LOWER,
        tick_upper=TICK_UPPER,
        liquidity=L,
        amount0_max=a0_max,
        amount1_max=a1_max,
        eth_value_wei=a0_max,
    )
    log.info(f"  result: {mint_res.status}")
    if mint_res.tx_hash:
        log.info(f"    tx: {mint_res.tx_hash}")
    if mint_res.error:
        log.error(f"    error: {mint_res.error}")
        return

    # ─── 6. Verify + persist tokenId ───────────────────────────
    if config.RISK.dry_run:
        log.info("\n[DRY_RUN] No state changed. Set DRY_RUN=false to execute.")
        return

    log.info("\n--- Post-mint state ---")
    log.info(f"  USDC: {base.balance(USDC_BASE)/1e6:.6f}")
    log.info(f"  ETH:  {base.w3.eth.get_balance(base.address)/1e18:.6f}")

    # Extract tokenId from mint receipt
    if mint_res.receipt is None:
        log.warning("  no receipt — cannot determine tokenId")
        return
    token_id = _extract_token_id(mint_res.receipt, base.address)
    if token_id is None:
        log.warning("  could not find POSM Transfer log in receipt")
        return
    log.info(f"  POSM tokenId: {token_id}")

    # Persist
    tracker = PnLTracker()
    tracker.record_lp_mint(
        token_id=token_id,
        pool_label="ETH/USDC",
        hook=HOOK_ETH_USDC,
        tick_lower=TICK_LOWER,
        tick_upper=TICK_UPPER,
        ts=datetime.utcnow().isoformat(),
        block=mint_res.receipt.blockNumber,
        tx=mint_res.tx_hash,
        initial_amount0=a0,
        initial_amount1=a1,
    )
    log.info(f"  saved to lp_positions table")


if __name__ == "__main__":
    main()
