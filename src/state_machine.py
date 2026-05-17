"""
State machine for vrp-agent.

Phase 1 (USE_LP_PARKING=False) — minimal, used for first live cycle:
  CASH_ON_HL ⇄ LONG_ON_HL
    USDC sits on Hyperliquid between trades.
    Trade open/close via HL SDK. No bridging, no LP.

Phase 2 (USE_LP_PARKING=True) — full hybrid:
  PARKED_IN_LP ─[entry signal]→ BRIDGING_TO_HL → LONG_ON_HL
  LONG_ON_HL   ─[exit signal] → BRIDGING_TO_BASE → PARKED_IN_LP
    Multi-step transitions with on-chain reconciliation.

On restart, agent reads on-chain state (LP NFT, HL position) and resolves
which state it's truly in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from . import config

log = logging.getLogger(__name__)


class State(str, Enum):
    # Phase 1 states
    CASH_ON_HL        = "CASH_ON_HL"        # USDC idle on HL, no position
    LONG_ON_HL        = "LONG_ON_HL"        # long ETH-PERP on HL

    # Phase 2 additional states
    PARKED_IN_LP      = "PARKED_IN_LP"      # USDC/EURC in LP NFT on Base
    BRIDGING_TO_HL    = "BRIDGING_TO_HL"    # transitioning out of LP into HL
    BRIDGING_TO_BASE  = "BRIDGING_TO_BASE"  # transitioning back to LP

    # Defensive mode: when VRP < 0 (crash regime), exit LP entirely
    # and stay as all-USDC on Base (no ETH price exposure)
    DEFENSIVE_CASH    = "DEFENSIVE_CASH"

    UNKNOWN = "UNKNOWN"
    ERROR   = "ERROR"


@dataclass
class StateSnapshot:
    """Single-instant reading of on-chain reality."""
    base_usdc:    int                  # raw 6-dec
    base_eurc:    int
    base_eth_wei: int
    lp_token_id:  int | None
    lp_liquidity: int
    hl_perp_size: float                # signed: + long, − short, 0 = flat
    hl_perp_entry: float
    hl_margin_usd: float

    def has_lp(self) -> bool:
        return self.lp_token_id is not None and self.lp_liquidity > 0

    def has_perp(self) -> bool:
        return abs(self.hl_perp_size) > 1e-9

    def fmt(self) -> str:
        return (
            f"base_USDC={self.base_usdc/1e6:.4f} EURC={self.base_eurc/1e6:.4f} "
            f"ETH={self.base_eth_wei/1e18:.6f} "
            f"LP={'NFT#'+str(self.lp_token_id) if self.has_lp() else 'none'} "
            f"HL_perp={self.hl_perp_size:+.4f}@{self.hl_perp_entry:.2f} "
            f"HL_margin=${self.hl_margin_usd:.2f}"
        )


def reconcile(snap: StateSnapshot) -> State:
    """
    Determine current state from on-chain reality.
    Trusts on-chain over any stored state file.
    """
    has_lp   = snap.has_lp()
    has_perp = snap.has_perp()
    if config.STRAT.use_lp_parking:
        if has_perp and not has_lp:
            return State.LONG_ON_HL
        if has_lp and not has_perp:
            return State.PARKED_IN_LP
        if has_lp and has_perp:
            # Both? Probably mid-transition or unexpected
            log.warning("inconsistent: has LP AND perp position")
            return State.LONG_ON_HL  # treat as in-trade
        # No LP, no perp — figure out where funds are
        if snap.base_usdc > 500_000:   # > $0.50 USDC on Base
            return State.DEFENSIVE_CASH   # parked as stable
        if snap.hl_margin_usd > 0.5:
            return State.CASH_ON_HL    # funds on HL but no position
        return State.UNKNOWN
    else:
        # Phase 1: ignore LP
        if has_perp:
            return State.LONG_ON_HL
        return State.CASH_ON_HL


# ─── Transition outcomes ────────────────────────────────────────────

@dataclass
class TransitionResult:
    success: bool
    new_state: State
    log: list[str]
    error: str | None = None
