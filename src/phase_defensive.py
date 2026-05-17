"""
Defensive transitions: PARKED_IN_LP ⇄ DEFENSIVE_CASH (all USDC on Base).

Triggered when VRP crosses below 0 — i.e., realized exceeds implied = the
options market is being caught off-guard by a crash. In this regime, our
LP has ~50% ETH exposure and bleeds with ETH drawdowns. So we exit LP
entirely and sit as USDC until VRP recovers above 0.

Pipeline is Base-only (no HL, no CCTP):

  run_to_defensive():
    1. POSM.burn(tokenId) → receive ETH + USDC
    2. Swap ETH → USDC (Uni v3, keep gas reserve)
    3. Record LP burn in SQLite
    → all USDC on Base wallet

  run_to_lp():
    1. Read current USDC + ETH balances
    2. Split: half USDC stays, half swaps to ETH
    3. Mint new POSM NFT in ETH/USDC pool
    4. Save tokenId in SQLite
    → back to PARKED_IN_LP

Run:
  DRY_RUN=false python -m src.phase_defensive --to-defensive
  DRY_RUN=false python -m src.phase_defensive --to-lp
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime

from . import config
from .on_chain.client import (
    BaseClient, USDC_BASE, NATIVE_ETH, HOOK_ETH_USDC, POSM,
    make_pool_key_eth_usdc, compute_pool_id, read_pool_slot0,
)
from .on_chain.lp_manager import LPManager
from .on_chain.swap_v3 import V3Swap
from .on_chain.liquidity_math import (
    liquidity_from_amounts, tick_to_sqrt_price_x96,
    amount0_from_liquidity, amount1_from_liquidity,
)
from .pnl_tracker import PnLTracker

log = logging.getLogger("phase_defensive")

# Reserves
GAS_RESERVE_ETH_WEI = 1_000_000_000_000_000  # 0.001 ETH
USDC_BUFFER_USD     = 0.5                     # keep $0.50 buffer outside LP

# Full-range ticks
TICK_LOWER = -887220
TICK_UPPER =  887220
SAFETY_BUFFER_BPS = 100  # 1% slippage tolerance


def run_to_defensive():
    """PARKED_IN_LP → DEFENSIVE_CASH: burn LP + swap ETH→USDC."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log.info("=" * 60)
    log.info("DEFENSIVE: PARKED_IN_LP → DEFENSIVE_CASH (all USDC)")
    log.info(f"  DRY_RUN: {config.RISK.dry_run}")
    log.info("=" * 60)

    base = BaseClient()
    lp = LPManager(base)
    swap = V3Swap(base)
    tracker = PnLTracker()

    active = tracker.active_lp()
    if not active:
        log.error("no active LP — already defensive?")
        return False
    token_id = active["token_id"]
    log.info(f"\n  active LP: tokenId {token_id} ({active['pool_label']})")

    pos = lp.read_position(token_id)
    if not pos.exists or pos.liquidity == 0:
        log.error(f"  LP {token_id} not active on-chain")
        return False

    pre_eth = base.w3.eth.get_balance(base.address)
    pre_usdc = base.balance(USDC_BASE)
    log.info(f"  Base ETH:  {pre_eth/1e18:.6f}")
    log.info(f"  Base USDC: ${pre_usdc/1e6:.4f}")

    # Step 1: burn LP
    log.info(f"\n--- Step 1: burn LP tokenId {token_id} ---")
    burn_res = lp.burn(token_id)
    log.info(f"  result: {burn_res.status}")
    if burn_res.error:
        log.error(f"  error: {burn_res.error}")
        return False
    if not config.RISK.dry_run:
        tracker.record_lp_burn(
            token_id=token_id,
            ts=datetime.utcnow().isoformat(),
            tx=burn_res.tx_hash or "",
        )
        # Wait for balance update (ETH back from LP)
        time.sleep(3)

    # Step 2: swap all ETH (above gas reserve) → USDC
    cur_eth = base.w3.eth.get_balance(base.address) if not config.RISK.dry_run else pre_eth + 10**15
    eth_to_swap = max(0, cur_eth - GAS_RESERVE_ETH_WEI)
    log.info(f"\n--- Step 2: swap {eth_to_swap/1e18:.6f} ETH → USDC ---")
    if eth_to_swap < 10_000_000_000_000:  # < 0.00001 ETH
        log.info("  too little ETH to swap, skipping")
    else:
        pre_swap_usdc = base.balance(USDC_BASE)
        swap_res = swap.eth_to_usdc(eth_to_swap)
        log.info(f"  result: {swap_res.status}")
        if swap_res.error:
            log.error(f"  error: {swap_res.error}")
            return False
        if not config.RISK.dry_run:
            poll_start = time.time()
            while time.time() - poll_start < 30:
                cur = base.balance(USDC_BASE)
                if cur >= pre_swap_usdc + 100_000:  # +$0.10
                    log.info(f"  USDC updated: ${cur/1e6:.4f} (+${(cur-pre_swap_usdc)/1e6:.4f})")
                    break
                time.sleep(1)

    log.info("\n" + "=" * 60)
    log.info("✓ Defensive position established")
    if not config.RISK.dry_run:
        log.info(f"  Final Base USDC: ${base.balance(USDC_BASE)/1e6:.4f}")
        log.info(f"  Final Base ETH:  {base.w3.eth.get_balance(base.address)/1e18:.6f} (gas reserve)")
    log.info("=" * 60)
    return True


def run_to_lp():
    """DEFENSIVE_CASH → PARKED_IN_LP: swap half USDC→ETH + mint LP."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log.info("=" * 60)
    log.info("RE-LP: DEFENSIVE_CASH → PARKED_IN_LP (mint new LP)")
    log.info(f"  DRY_RUN: {config.RISK.dry_run}")
    log.info("=" * 60)

    base = BaseClient()
    lp = LPManager(base)
    swap = V3Swap(base)
    tracker = PnLTracker()

    # Sanity: must not have active LP
    active = tracker.active_lp()
    if active:
        log.warning(f"  WARNING: existing active LP in SQLite (tokenId {active['token_id']})")
        # Verify on chain
        pos = lp.read_position(active["token_id"])
        if pos.exists and pos.liquidity > 0:
            log.error("  LP still on chain — already in PARKED_IN_LP. Aborting.")
            return False

    cur_usdc = base.balance(USDC_BASE)
    cur_eth = base.w3.eth.get_balance(base.address)
    log.info(f"\n  Base USDC: ${cur_usdc/1e6:.4f}")
    log.info(f"  Base ETH:  {cur_eth/1e18:.6f}")

    if cur_usdc < 1_000_000:  # < $1
        log.error("  insufficient USDC to mint LP (< $1)")
        return False

    # Compute split: half USDC stays, half swap to ETH
    # Leave USDC_BUFFER_USD on agent wallet
    deployable_usdc = cur_usdc - int(USDC_BUFFER_USD * 1e6)
    usdc_for_lp   = deployable_usdc // 2
    usdc_for_swap = deployable_usdc - usdc_for_lp
    log.info(f"\n  reserve buffer:    ${USDC_BUFFER_USD:.2f}")
    log.info(f"  USDC stays for LP: ${usdc_for_lp/1e6:.4f}")
    log.info(f"  USDC → swap → ETH: ${usdc_for_swap/1e6:.4f}")

    # Step 1: swap USDC → ETH
    log.info(f"\n--- Step 1: swap USDC → ETH ---")
    pre_swap_eth = base.w3.eth.get_balance(base.address)
    swap_res = swap.usdc_to_eth(usdc_for_swap)
    log.info(f"  result: {swap_res.status}")
    if swap_res.error:
        log.error(f"  error: {swap_res.error}")
        return False
    if not config.RISK.dry_run:
        # Poll ETH update
        log.info(f"  polling ETH balance update…")
        poll_start = time.time()
        while time.time() - poll_start < 30:
            cur = base.w3.eth.get_balance(base.address)
            if cur >= pre_swap_eth + 100_000_000_000_000:  # +0.0001 ETH
                log.info(f"  ETH updated: {cur/1e18:.6f}")
                break
            time.sleep(1)

    # Step 2: mint LP
    log.info(f"\n--- Step 2: mint LP ETH/USDC ---")
    eth_for_lp = base.w3.eth.get_balance(base.address) - GAS_RESERVE_ETH_WEI
    log.info(f"  ETH for LP: {eth_for_lp/1e18:.6f}")
    log.info(f"  USDC for LP: ${usdc_for_lp/1e6:.4f}")

    pk = make_pool_key_eth_usdc()
    pool_id = compute_pool_id(pk)
    slot0 = read_pool_slot0(base, pool_id)
    sqrt_p = slot0["sqrt_price_x96"]
    sqrt_pl = tick_to_sqrt_price_x96(TICK_LOWER)
    sqrt_pu = tick_to_sqrt_price_x96(TICK_UPPER)

    L = liquidity_from_amounts(sqrt_p, sqrt_pl, sqrt_pu, eth_for_lp, usdc_for_lp)
    a0 = amount0_from_liquidity(sqrt_p, sqrt_pu, L)
    a1 = amount1_from_liquidity(sqrt_pl, sqrt_p, L)
    a0_max = a0 + a0 * SAFETY_BUFFER_BPS // 10_000
    a1_max = a1 + a1 * SAFETY_BUFFER_BPS // 10_000
    log.info(f"  L: {L:,}, ETH={a0/1e18:.6f}, USDC=${a1/1e6:.4f}")

    lp.setup_approvals([USDC_BASE])

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
    if mint_res.error:
        log.error(f"  error: {mint_res.error}")
        return False

    if mint_res.receipt and not config.RISK.dry_run:
        from .bootstrap_pool2 import _extract_token_id
        new_token_id = _extract_token_id(mint_res.receipt, base.address)
        if new_token_id:
            log.info(f"  ✓ new LP tokenId: {new_token_id}")
            tracker.record_lp_mint(
                token_id=new_token_id,
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

    log.info("\n" + "=" * 60)
    log.info("✓ Re-LP complete — back to PARKED_IN_LP")
    log.info("=" * 60)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--to-defensive", action="store_true",
                         help="burn LP + swap ETH→USDC")
    parser.add_argument("--to-lp", action="store_true",
                         help="swap half USDC→ETH + mint LP")
    args = parser.parse_args()
    if args.to_defensive:
        run_to_defensive()
    elif args.to_lp:
        run_to_lp()
    else:
        parser.print_help()
