"""
Phase 1 transitions: CASH_ON_HL ⇄ LONG_ON_HL.

These are higher-level orchestrators that combine multiple module calls
and persist state to SQLite. They're idempotent — safe to retry from
any partial state.
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import config
from .state_machine import State, StateSnapshot

log = logging.getLogger(__name__)


def snapshot(client, lp_mgr, hl_exec, tracker=None) -> StateSnapshot:
    """Read complete on-chain state for the agent wallet.

    LP detection:
      - Read active tokenId from SQLite (lp_positions table)
      - Verify it still exists on-chain (catches manual burns / bugs)
      - If SQLite says active but chain says burned → log warning + treat
        as no LP (and mark burnt in SQLite below — left for caller)
    """
    from .on_chain.client import USDC_BASE, EURC_BASE

    base_usdc = client.balance(USDC_BASE)
    base_eurc = client.balance(EURC_BASE)
    base_eth  = client.w3.eth.get_balance(client.address)

    # ─── LP detection: SQLite + on-chain verify ───────────────
    lp_token_id = None
    lp_liquidity = 0
    if tracker is not None:
        active = tracker.active_lp()
        if active:
            pos = lp_mgr.read_position(active["token_id"])
            if pos.exists and pos.liquidity > 0:
                lp_token_id  = active["token_id"]
                lp_liquidity = pos.liquidity
            else:
                log.warning(f"  SQLite says LP {active['token_id']} active "
                             f"but chain says exists={pos.exists}, L={pos.liquidity}")

    # HL position
    pos_hl = hl_exec.get_position()
    if pos_hl is not None:
        hl_perp_size  = pos_hl.size_eth
        hl_perp_entry = pos_hl.entry_px
    else:
        hl_perp_size  = 0.0
        hl_perp_entry = 0.0

    hl_margin = hl_exec.get_usdc_balance()

    # Add spot USDC for unified-account view
    try:
        import requests
        r = requests.post(
            f"{config.HL.api_url}/info",
            json={"type": "spotClearinghouseState", "user": client.address},
            timeout=10,
        )
        balances = r.json().get("balances", []) if r.status_code == 200 else []
        spot_usdc = next((float(b["total"]) for b in balances
                           if b.get("coin") == "USDC"), 0.0)
        hl_margin += spot_usdc
    except Exception as e:
        log.warning(f"  spot balance fetch failed: {e}")

    # HyperEVM USDC — funds in transit between Base and HL.
    # Used by reconcile() to detect IN_TRANSIT_TO_HL / IN_TRANSIT_TO_BASE
    # states when a CCTP/HL bridge leg stalled.
    hyperevm_usdc = 0
    try:
        from .phase2_forward import hyperevm_usdc_balance
        hyperevm_usdc = hyperevm_usdc_balance(client.address)
    except Exception as e:
        log.warning(f"  HyperEVM USDC fetch failed: {e}")

    return StateSnapshot(
        base_usdc=base_usdc,
        base_eurc=base_eurc,
        base_eth_wei=base_eth,
        lp_token_id=lp_token_id,
        lp_liquidity=lp_liquidity,
        hl_perp_size=hl_perp_size,
        hl_perp_entry=hl_perp_entry,
        hl_margin_usd=hl_margin,
        hyperevm_usdc=hyperevm_usdc,
    )


# ─── Phase 1 transitions ────────────────────────────────────────────

def enter_long_hl(hl_exec, tracker, snap: StateSnapshot, decision_reason: str):
    """
    Open long ETH-PERP on Hyperliquid using available HL USDC.
    Position size = min(MAX_POSITION_USD, available margin × 0.95).
    """
    available = snap.hl_margin_usd * 0.95   # leave 5% buffer
    size_usd = min(config.RISK.max_position_usd, available)
    if size_usd < 1.0:
        log.warning(f"  available HL margin too low: ${snap.hl_margin_usd:.4f}")
        return False, "insufficient HL margin"

    log.info(f"  opening LONG ETH-PERP size=${size_usd:.2f}")
    result = hl_exec.open_long(size_usd)
    if result.get("status") not in ("dry_run", "submitted"):
        return False, f"open_long failed: {result}"

    # Log trade open
    tracker.open_trade(
        ts=datetime.utcnow().isoformat(),
        price=result.get("mid_price", 0.0),
        size_eth=result.get("size_eth", 0.0),
        reason=decision_reason,
        dry_run=config.RISK.dry_run,
        raw=result,
    )
    return True, None


def exit_long_hl(hl_exec, tracker, snap: StateSnapshot, decision_reason: str):
    """Close current ETH-PERP position via market order."""
    if not snap.has_perp():
        return False, "no perp position to close"

    log.info(f"  closing LONG ETH-PERP (size={snap.hl_perp_size:+.4f})")
    result = hl_exec.close_position()
    if result.get("status") not in ("dry_run", "submitted", "noop"):
        return False, f"close failed: {result}"

    # Log trade close — use current mid as exit price
    exit_price = hl_exec.get_mid_price()
    trade_id = tracker.open_trade_id()
    if trade_id and exit_price == exit_price:  # nan-safe
        tracker.close_trade(
            trade_id=trade_id,
            ts=datetime.utcnow().isoformat(),
            price=exit_price,
            reason=decision_reason,
            raw=result,
        )
    return True, None
