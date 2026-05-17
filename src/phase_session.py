"""
Session-strategy orchestrators.

Wraps existing pipelines (phase2_forward, phase2_reverse, phase_defensive,
transitions) into clean session-aware entry/exit functions.

  enter_long(state, mode, session_date)
    Dispatches based on current state:
      CASH_ON_HL      → hl_exec.open_long()            (~3s)
      PARKED_IN_LP    → phase2_forward.run_forward()   (~60s)
      DEFENSIVE_CASH  → phase2_forward.run_forward_from_cash()  (~45s)

  exit_long(target)
    target='cash' → hl_exec.close_position(), capital stays on HL
    target='lp'   → phase2_reverse.run_reverse(), full pipeline back to LP
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import config
from . import phase2_forward
from . import phase2_reverse
from . import state_machine as sm
from . import transitions
from .hl_executor import HLExecutor
from .on_chain.client import BaseClient
from .on_chain.lp_manager import LPManager
from .pnl_tracker import PnLTracker

log = logging.getLogger(__name__)


def enter_long(
    state: sm.State,
    mode: str,
    session_date: str | None,
    entry_reason: str,
) -> bool:
    """Open long position, routing via current state. Returns True on success."""
    if state == sm.State.CASH_ON_HL:
        log.info(f"  ▶ enter_long path: CASH_ON_HL → LONG_ON_HL (direct)")
        hl_exec = HLExecutor()
        tracker = PnLTracker()
        base    = BaseClient()
        lp_mgr  = LPManager(base)
        snap = transitions.snapshot(base, lp_mgr, hl_exec, tracker)
        ok, err = transitions.enter_long_hl(hl_exec, tracker, snap, entry_reason)
        if ok and not config.RISK.dry_run:
            # Backfill entry_mode + session_date on the open trade
            tid = tracker.open_trade_id()
            if tid:
                tracker._conn.execute(
                    "UPDATE trades SET entry_mode=?, session_date=? WHERE id=?",
                    (mode, session_date, tid),
                )
                tracker._conn.commit()
        log.info(f"  enter_long → {'OK' if ok else 'FAIL: ' + (err or '')}")
        return ok

    if state == sm.State.PARKED_IN_LP:
        log.info(f"  ▶ enter_long path: PARKED_IN_LP → LONG_ON_HL (phase2_forward)")
        try:
            phase2_forward.run_forward(
                entry_mode=mode,
                session_date=session_date,
                entry_reason=entry_reason,
            )
            return True
        except Exception:
            log.exception("  ✗ phase2_forward FAILED")
            return False

    if state == sm.State.DEFENSIVE_CASH:
        log.info(f"  ▶ enter_long path: DEFENSIVE_CASH → LONG_ON_HL (forward_from_cash)")
        try:
            phase2_forward.run_forward_from_cash(
                entry_mode=mode,
                session_date=session_date,
                entry_reason=entry_reason,
            )
            return True
        except Exception:
            log.exception("  ✗ phase2_forward_from_cash FAILED")
            return False

    log.error(f"  ✗ enter_long: unsupported state {state.value}")
    return False


def exit_long(target: str, exit_reason: str) -> bool:
    """Close long position and route capital.

    Args:
        target: 'cash' (stay on HL as CASH_ON_HL) or 'lp' (full reverse to PARKED_IN_LP)
        exit_reason: text reason for SQLite
    """
    if target == "cash":
        log.info(f"  ▶ exit_long path: LONG_ON_HL → CASH_ON_HL (direct close)")
        hl_exec = HLExecutor()
        tracker = PnLTracker()
        base    = BaseClient()
        lp_mgr  = LPManager(base)
        snap = transitions.snapshot(base, lp_mgr, hl_exec, tracker)
        ok, err = transitions.exit_long_hl(hl_exec, tracker, snap, exit_reason)
        log.info(f"  exit_long → {'OK' if ok else 'FAIL: ' + (err or '')}")
        return ok

    if target == "lp":
        log.info(f"  ▶ exit_long path: LONG_ON_HL → PARKED_IN_LP (phase2_reverse)")
        try:
            phase2_reverse.run_reverse()
            return True
        except Exception:
            log.exception("  ✗ phase2_reverse FAILED")
            return False

    log.error(f"  ✗ exit_long: unknown target '{target}'")
    return False
