"""
Phase 1 bootstrap: park agent's USDC into USDC/EURC LP.

Allocation rule (auto, no prompt):
  Take TARGET_USDC of Base USDC →
    swap half → EURC
    mint full-range LP with both sides

For our agent (start $10.50 on Base, $5 on HL):
  TARGET_USDC = $5 (matches HL reference balance — user's rule)
  → swap $2.50 USDC → ~$2.14 EURC
  → mint LP with $2.50 USDC + $2.14 EURC

Run:
  python -m src.bootstrap_pool1            # dry-run preview
  DRY_RUN=false python -m src.bootstrap_pool1   # actually execute
"""
from __future__ import annotations

import logging
import time

from . import config
from .on_chain.client import (
    BaseClient, USDC_BASE, EURC_BASE, POSM, PERMIT2,
    make_pool_key, compute_pool_id, read_pool_slot0,
)
from .on_chain.lp_manager import LPManager
from .on_chain.swap import SwapManager
from .on_chain.liquidity_math import (
    liquidity_from_amounts, tick_to_sqrt_price_x96,
    amount0_from_liquidity, amount1_from_liquidity,
)

log = logging.getLogger("bootstrap")

# Target allocation
# NOTE: pool LP is only ~$11.7 (user's existing position). Larger swaps
# cause severe price impact. For first live test use $0.50 (small enough
# to keep pool impact <5%). Increase after first successful mint.
TARGET_USDC_VALUE = 500_000  # $0.50 total (raw 6-dec)
SAFETY_BUFFER_BPS = 200       # 2% slippage tolerance on mint amounts

# Full-range ticks for tickSpacing=60
TICK_LOWER = -887220
TICK_UPPER =  887220


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log.info("=" * 60)
    log.info("Phase 1 bootstrap: park agent capital into USDC/EURC LP")
    log.info(f"  TARGET_USDC = ${TARGET_USDC_VALUE/1e6:.4f}")
    log.info(f"  DRY_RUN     = {config.RISK.dry_run}")
    log.info("=" * 60)

    base = BaseClient()
    lp = LPManager(base)
    sm = SwapManager(base)

    # ─── 1. Pre-flight checks ──────────────────────────────────
    usdc_bal = base.balance(USDC_BASE)
    eurc_bal = base.balance(EURC_BASE)
    eth_bal  = base.w3.eth.get_balance(base.address)
    log.info(f"\n--- Pre-flight ---")
    log.info(f"  USDC: {usdc_bal/1e6:.4f}")
    log.info(f"  EURC: {eurc_bal/1e6:.4f}")
    log.info(f"  ETH:  {eth_bal/1e18:.6f}")
    if usdc_bal < TARGET_USDC_VALUE:
        log.error(f"FAIL: insufficient USDC (need ${TARGET_USDC_VALUE/1e6}, have ${usdc_bal/1e6})")
        return
    if eth_bal < int(0.003 * 1e18):
        log.error(f"FAIL: ETH balance too low for ~5-7 txes (need 0.003+, have {eth_bal/1e18:.6f})")
        return

    # ─── 2. Read pool state ────────────────────────────────────
    pk = make_pool_key()
    pool_id = compute_pool_id(pk)
    slot0 = read_pool_slot0(base, pool_id)
    sqrt_p = slot0["sqrt_price_x96"]
    cur_tick = slot0["tick"]
    log.info(f"\n--- Pool state ---")
    log.info(f"  poolId: 0x{pool_id.hex()}")
    log.info(f"  sqrtPriceX96: {sqrt_p}")
    log.info(f"  current tick: {cur_tick}")
    price_t1_per_t0 = (sqrt_p / (1 << 96)) ** 2
    log.info(f"  price (USDC per EURC): {price_t1_per_t0:.6f}")

    # ─── 3. Setup approvals (Permit2 + ERC20) ──────────────────
    log.info(f"\n--- Approvals ---")
    log.info("Setup ERC20 → Permit2 → POSM chain for USDC + EURC:")
    lp.setup_approvals([USDC_BASE, EURC_BASE])

    # Also approve PoolSwapTest for USDC swap leg
    from .on_chain.swap import POOL_SWAP_TEST
    cur = base.allowance(USDC_BASE, POOL_SWAP_TEST)
    if cur < (1 << 200):
        log.info(f"  ERC20.approve(USDC → PoolSwapTest, max)")
        base.approve(USDC_BASE, POOL_SWAP_TEST, 2**256 - 1)
    else:
        log.info(f"  USDC → PoolSwapTest already approved")

    # ─── 4. Swap half USDC → EURC ──────────────────────────────
    usdc_to_swap = TARGET_USDC_VALUE // 2
    usdc_for_lp  = TARGET_USDC_VALUE - usdc_to_swap
    log.info(f"\n--- Swap step ---")
    log.info(f"  swap {usdc_to_swap/1e6:.4f} USDC → EURC (target rate ≈ {1/price_t1_per_t0:.4f})")

    pre_eurc = base.balance(EURC_BASE)
    res = sm.usdc_to_eurc(usdc_to_swap)
    log.info(f"  swap result: {res.status}")
    if res.tx_hash:
        log.info(f"    tx: {res.tx_hash}")
    if res.error:
        log.error(f"    error: {res.error}")
        return

    if config.RISK.dry_run:
        log.info(f"  [DRY_RUN] estimating post-swap EURC: ~{usdc_to_swap/price_t1_per_t0/1e6:.4f}")
        eurc_received = int(usdc_to_swap / price_t1_per_t0 * 0.995)  # estimate w/ 0.5% slippage
    else:
        time.sleep(2)
        post_eurc = base.balance(EURC_BASE)
        eurc_received = post_eurc - pre_eurc
        log.info(f"  actual EURC received: {eurc_received/1e6:.6f}")

    # ─── 5. Compute L from final amounts ───────────────────────
    sqrt_pl = tick_to_sqrt_price_x96(TICK_LOWER)
    sqrt_pu = tick_to_sqrt_price_x96(TICK_UPPER)
    # amount0 = EURC, amount1 = USDC
    L = liquidity_from_amounts(sqrt_p, sqrt_pl, sqrt_pu, eurc_received, usdc_for_lp)
    # Estimated amounts POSM will pull (using same L)
    a0 = amount0_from_liquidity(sqrt_p, sqrt_pu, L)
    a1 = amount1_from_liquidity(sqrt_pl, sqrt_p, L)
    # Apply safety buffer (mint may request slightly more due to rounding)
    a0_max = a0 + a0 * SAFETY_BUFFER_BPS // 10_000
    a1_max = a1 + a1 * SAFETY_BUFFER_BPS // 10_000

    log.info(f"\n--- L computation ---")
    log.info(f"  liquidity L: {L:,}")
    log.info(f"  amount0 (EURC) POSM will pull: ~{a0/1e6:.6f} (max={a0_max/1e6:.6f})")
    log.info(f"  amount1 (USDC) POSM will pull: ~{a1/1e6:.6f} (max={a1_max/1e6:.6f})")

    # ─── 6. Mint LP ────────────────────────────────────────────
    log.info(f"\n--- Mint LP ---")
    mint_res = lp.mint(
        tick_lower=TICK_LOWER,
        tick_upper=TICK_UPPER,
        liquidity=L,
        amount0_max=a0_max,
        amount1_max=a1_max,
    )
    log.info(f"  mint result: {mint_res.status}")
    if mint_res.tx_hash:
        log.info(f"    tx: {mint_res.tx_hash}")
    if mint_res.error:
        log.error(f"    error: {mint_res.error}")
        return

    # ─── 7. Verify ─────────────────────────────────────────────
    if not config.RISK.dry_run:
        log.info(f"\n--- Post-mint state ---")
        log.info(f"  USDC: {base.balance(USDC_BASE)/1e6:.6f}")
        log.info(f"  EURC: {base.balance(EURC_BASE)/1e6:.6f}")
        log.info(f"  ETH:  {base.w3.eth.get_balance(base.address)/1e18:.6f}")
        log.info("  (check Uniswap UI or BaseScan for new POSM NFT)")
    else:
        log.info(f"\n[DRY_RUN] No state changed. Re-run with DRY_RUN=false to execute.")


if __name__ == "__main__":
    main()
