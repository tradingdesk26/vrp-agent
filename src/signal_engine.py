"""
Signal engine: Parkinson realized vol + VRP + state machine.

Level-based strategy (calibrated +58%/2y, Sharpe 2.20):
  ENTRY (CASH → LONG): VRP crosses UP through vrp_entry_level (+30) AND
                       R_long < regime_max_r_long (60%) AND cooldown elapsed
  EXIT  (LONG → CASH): VRP crosses DOWN through vrp_exit_level (+6) OR
                       max_hold reached OR stop-loss hit
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from . import config


class State(str, Enum):
    CASH = "CASH"
    LONG = "LONG"


@dataclass
class SignalSnapshot:
    """Latest computed signal values (one point in time)."""
    timestamp:       pd.Timestamp
    price:           float
    r_short:         float
    r_long:          float
    dvol:            float
    vrp:             float        # dvol − r_long
    in_quiet_regime: bool         # r_long < regime_max

    def fmt(self) -> str:
        return (
            f"price=${self.price:.2f}  "
            f"R_s={self.r_short:.1f}% R_L={self.r_long:.1f}%  "
            f"DVOL={self.dvol:.1f}%  "
            f"VRP={self.vrp:+.2f}  "
            f"quiet={self.in_quiet_regime}"
        )


@dataclass
class Decision:
    action: str   # "ENTER_LONG", "EXIT_LONG", "HOLD"
    reason: str


def parkinson(high: pd.Series, low: pd.Series, win: int) -> pd.Series:
    """Annualized Parkinson realized vol (%) over rolling `win` hours."""
    c   = 1.0 / (4.0 * np.log(2.0))
    ann = 365.0 * 24.0
    x = np.log(high / low).replace([np.inf, -np.inf], np.nan).pow(2)
    return np.sqrt(c * x.rolling(win, min_periods=win).mean()) * np.sqrt(ann) * 100.0


def compute_snapshot(df: pd.DataFrame) -> SignalSnapshot:
    """Compute current signal values from DataFrame of hourly bars."""
    s = config.STRAT
    df = df.copy().sort_values("datetime").reset_index(drop=True)
    df["r_short"] = parkinson(df["high"], df["low"], s.win_short_h)
    df["r_long"]  = parkinson(df["high"], df["low"], s.win_long_h)
    df["vrp"]     = df["dvol"] - df["r_long"]

    last = df.iloc[-1]
    return SignalSnapshot(
        timestamp=last["datetime"],
        price=float(last["close"]),
        r_short=float(last["r_short"]),
        r_long=float(last["r_long"]),
        dvol=float(last["dvol"]),
        vrp=float(last["vrp"]),
        in_quiet_regime=bool(last["r_long"] < s.regime_max_r_long),
    )


def decide(
    state: State,
    snap: SignalSnapshot,
    hours_since_last_entry: int,
    hours_in_position: int,
    pnl_pct: float,
    vrp_prev: float | None,
) -> Decision:
    """Apply level-based state machine rules.

    Level-cross detection requires `vrp_prev` (last iteration's VRP):
      cross UP   through L: vrp_prev <= L  AND  vrp_now > L
      cross DOWN through L: vrp_prev >= L  AND  vrp_now < L
    """
    s = config.STRAT

    if state == State.CASH:
        # Regime gate
        if not snap.in_quiet_regime:
            return Decision("HOLD",
                            f"R_long {snap.r_long:.1f}% ≥ regime_max "
                            f"{s.regime_max_r_long}% (not quiet)")
        # Cooldown gate
        if hours_since_last_entry < s.entry_cooldown_h:
            return Decision("HOLD",
                            f"cooldown {hours_since_last_entry}h < "
                            f"{s.entry_cooldown_h}h")
        # Cross-up entry trigger
        if vrp_prev is None:
            return Decision("HOLD", "no prev VRP, can't detect cross")
        crossed_up = vrp_prev <= s.vrp_entry_level and snap.vrp > s.vrp_entry_level
        if not crossed_up:
            return Decision("HOLD",
                            f"VRP {snap.vrp:+.2f} (prev {vrp_prev:+.2f}) — "
                            f"no cross-up through +{s.vrp_entry_level}")
        return Decision("ENTER_LONG",
                        f"VRP crossed UP +{s.vrp_entry_level}: "
                        f"{vrp_prev:+.2f} → {snap.vrp:+.2f}, "
                        f"R_long={snap.r_long:.1f}% < {s.regime_max_r_long}")

    elif state == State.LONG:
        # Cross-down exit trigger
        if vrp_prev is not None:
            crossed_down = (vrp_prev >= s.vrp_exit_level
                             and snap.vrp < s.vrp_exit_level)
            if crossed_down:
                return Decision("EXIT_LONG",
                                f"VRP crossed DOWN +{s.vrp_exit_level}: "
                                f"{vrp_prev:+.2f} → {snap.vrp:+.2f}")
        # Safety exits
        if hours_in_position >= s.max_hold_hours:
            return Decision("EXIT_LONG",
                            f"max_hold: {hours_in_position}h ≥ "
                            f"{s.max_hold_hours}h")
        if pnl_pct <= s.stop_loss_pct:
            return Decision("EXIT_LONG",
                            f"stop-loss: pnl={pnl_pct:+.2f}% ≤ "
                            f"{s.stop_loss_pct}%")
        return Decision("HOLD",
                        f"in position {hours_in_position}h, "
                        f"pnl={pnl_pct:+.2f}%, VRP={snap.vrp:+.2f}")

    return Decision("HOLD", f"unknown state {state}")
