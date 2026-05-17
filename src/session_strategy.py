"""
Session-timing strategy decision function.

Three VRP zones:
  VRP ≤ 0:        between sessions sit in CASH_ON_HL (no LP — daily bridge cost
                  would eat capital)
  0 < VRP ≤ 30:   between sessions PARKED_IN_LP (collect fees)
  VRP > 30:       persistent LONG_ON_HL (override session timing, hold until VRP < 6)

Two timing triggers (when not in persistent mode):
  hour == 20 UTC and !today_session_done  →  ENTER_SESSION_LONG
  hour == 22 UTC and in session-long       →  EXIT_SESSION_LONG

Plus stop-loss (-5%) and cross-zero defensive triggers.

Pure function — no side effects, no I/O. Easy to unit test.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from . import state_machine as sm


SESSION_ENTRY_HOUR = 20            # UTC
SESSION_EXIT_HOUR  = 22            # UTC
VRP_PERSISTENT_ENTRY = 30.0        # cross-up triggers persistent-long
VRP_PERSISTENT_EXIT  = 6.0         # cross-down exits persistent-long
STOP_LOSS_PCT        = 0.05        # -5% drawdown stop


class Decision(str, Enum):
    HOLD                  = "HOLD"
    ENTER_SESSION_LONG    = "ENTER_SESSION_LONG"
    ENTER_PERSISTENT_LONG = "ENTER_PERSISTENT_LONG"
    UPGRADE_TO_PERSISTENT = "UPGRADE_TO_PERSISTENT"   # metadata flip, no on-chain action
    EXIT_LONG             = "EXIT_LONG"
    MOVE_LP_TO_DEFENSIVE  = "MOVE_LP_TO_DEFENSIVE"
    MOVE_DEFENSIVE_TO_LP  = "MOVE_DEFENSIVE_TO_LP"


@dataclass
class Action:
    decision: Decision
    reason: str
    # For EXIT_LONG: where to route post-exit. 'lp' | 'cash' | None
    post_route: Optional[str] = None


def decide(
    state: sm.State,
    vrp_now: Optional[float],
    vrp_prev: Optional[float],
    hour_utc: int,
    today_session_done: bool,
    entry_mode: Optional[str],
    pnl_pct: Optional[float] = None,
) -> Action:
    """
    Returns an Action describing what the agent should do.

    Args:
        state: current reconciled chain state
        vrp_now: latest VRP value (None if signal not ready)
        vrp_prev: prior poll VRP value (for cross detection)
        hour_utc: current UTC hour (0-23)
        today_session_done: True if a session-mode trade was opened today already
        entry_mode: 'session' | 'persistent' | None (when in LONG_ON_HL)
        pnl_pct: current position PnL as fraction (e.g. -0.03 = -3%)
    """
    vrp = vrp_now  # short alias
    vp  = vrp_prev

    # ─── In LONG: check stop, persistent exit, session exit ────────
    if state == sm.State.LONG_ON_HL:
        # Stop-loss takes precedence over everything
        if pnl_pct is not None and pnl_pct <= -STOP_LOSS_PCT:
            target = "lp" if (vrp is not None and vrp > 0) else "cash"
            return Action(Decision.EXIT_LONG,
                          f"stop_loss {pnl_pct*100:+.2f}%",
                          post_route=target)

        if entry_mode == "persistent":
            # Exit when VRP crosses BELOW the persistent-exit threshold
            if (vp is not None and vrp is not None
                    and vp >= VRP_PERSISTENT_EXIT and vrp < VRP_PERSISTENT_EXIT):
                target = "lp" if vrp > 0 else "cash"
                return Action(Decision.EXIT_LONG,
                              f"persistent_exit vrp {vp:+.1f}→{vrp:+.1f}",
                              post_route=target)
            return Action(Decision.HOLD, f"persistent_in_long vrp={vrp:+.1f}")

        # entry_mode == "session" (or None — treat as session by default)
        # Upgrade to persistent if VRP crosses UP through 30 during session
        if (vp is not None and vrp is not None
                and vp <= VRP_PERSISTENT_ENTRY and vrp > VRP_PERSISTENT_ENTRY):
            return Action(Decision.UPGRADE_TO_PERSISTENT,
                          f"vrp_cross_up_30 {vp:+.1f}→{vrp:+.1f}")

        if hour_utc == SESSION_EXIT_HOUR:
            target = "lp" if (vrp is not None and vrp > 0) else "cash"
            return Action(Decision.EXIT_LONG,
                          f"session_exit_22h",
                          post_route=target)

        return Action(Decision.HOLD, f"session_in_long hour={hour_utc}")

    # ─── Not in LONG ──────────────────────────────────────────────
    # Persistent entry: VRP crosses UP through 30
    if (vp is not None and vrp is not None
            and vp <= VRP_PERSISTENT_ENTRY and vrp > VRP_PERSISTENT_ENTRY):
        return Action(Decision.ENTER_PERSISTENT_LONG,
                      f"vrp_cross_up_30 {vp:+.1f}→{vrp:+.1f}")

    # Session entry: 20 UTC, not yet today
    if hour_utc == SESSION_ENTRY_HOUR and not today_session_done:
        return Action(Decision.ENTER_SESSION_LONG, f"session_20h")

    # Cross-zero routing for idle LP/DEFENSIVE states
    if state == sm.State.PARKED_IN_LP \
            and vp is not None and vrp is not None \
            and vp > 0 and vrp <= 0:
        return Action(Decision.MOVE_LP_TO_DEFENSIVE,
                      f"vrp_cross_down_0 {vp:+.1f}→{vrp:+.1f}")

    if state == sm.State.DEFENSIVE_CASH \
            and vp is not None and vrp is not None \
            and vp <= 0 and vrp > 0:
        return Action(Decision.MOVE_DEFENSIVE_TO_LP,
                      f"vrp_cross_up_0 {vp:+.1f}→{vrp:+.1f}")

    return Action(Decision.HOLD, f"idle state={state.value} hour={hour_utc}")
