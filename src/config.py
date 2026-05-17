"""
Strategy and runtime config.

Calibrated on 2-year ETH OHLC + DVOL backtest (vol-service/backtest/):
  Strategy: level-based VRP cross
  ENTRY: VRP crosses UP through +30 AND R_long(72h) < 60%
  EXIT:  VRP crosses DOWN through +6
  Result: +58% over 2y, Sharpe 2.20, 13 trades, 61.5% win rate
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR  = ROOT / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")


@dataclass
class StrategyConfig:
    # ─── Parkinson windows ─────────────────────────────────
    win_short_h: int = 6
    win_long_h:  int = 72

    # ─── Level-based entry/exit ───────────────────────────
    vrp_entry_level:  float = 30.0       # ENTER LONG when VRP crosses UP through this
    vrp_exit_level:   float = 6.0        # EXIT LONG when VRP crosses DOWN through this
    regime_max_r_long: float = 60.0      # R_long(72h) ceiling for "quiet" regime
    entry_cooldown_h: int   = 24         # min hours between entries

    # ─── Safety exits ─────────────────────────────────────
    max_hold_hours:  int   = 168         # 7 days timeout
    stop_loss_pct:   float = -10.0

    # ─── Polling cadence ──────────────────────────────────
    poll_interval_sec: int = 60          # check signals every minute

    # ─── Architecture mode ────────────────────────────────
    # False (Phase 1):  USDC sits idle on HL between trades
    # True  (Phase 2): USDC parked in ARMS LP on Base, bridged
    #                  to HL on trigger
    # Agent runs with True now (tokenId 2334439 in ETH/USDC pool).
    # When VRP > 30 fires, Phase 2 transitions (burn → swap → CCTP →
    # HL open) need to be live OR agent will sit on HOLD.
    use_lp_parking: bool = True


@dataclass
class HyperliquidConfig:
    private_key:     str  = os.getenv("HL_PRIVATE_KEY", "")
    api_wallet_key:  str  = os.getenv("HL_API_WALLET_KEY", "")
    api_url:         str  = "https://api.hyperliquid.xyz"
    perp_symbol:     str  = "ETH"        # ETH-PERP

    def __post_init__(self):
        # strip 0x prefix if present
        if self.private_key.startswith("0x"):
            self.private_key = self.private_key[2:]
        if self.api_wallet_key.startswith("0x"):
            self.api_wallet_key = self.api_wallet_key[2:]


@dataclass
class RiskConfig:
    dry_run:              bool  = os.getenv("DRY_RUN", "true").lower() != "false"
    max_position_usd:     float = float(os.getenv("MAX_POSITION_USD", "500"))
    daily_loss_limit_usd: float = float(os.getenv("DAILY_LOSS_LIMIT_USD", "50"))


@dataclass
class DeribitConfig:
    base_url:        str = "https://www.deribit.com/api/v2"
    instrument_btc:  str = "BTC-PERPETUAL"
    instrument_eth:  str = "ETH-PERPETUAL"
    history_hours:   int = 10 * 24       # need 10d to warmup 72h window


STRAT  = StrategyConfig()
HL     = HyperliquidConfig()
RISK   = RiskConfig()
DERIB  = DeribitConfig()


def banner() -> str:
    return (
        f"vrp-agent config:\n"
        f"  DRY_RUN              = {RISK.dry_run}\n"
        f"  MAX_POSITION_USD     = ${RISK.max_position_usd:.2f}\n"
        f"  DAILY_LOSS_LIMIT     = ${RISK.daily_loss_limit_usd:.2f}\n"
        f"  STRAT windows        = {STRAT.win_short_h}h / {STRAT.win_long_h}h (Parkinson)\n"
        f"  ENTRY: VRP cross UP +{STRAT.vrp_entry_level} AND "
        f"R_long < {STRAT.regime_max_r_long}%\n"
        f"  EXIT:  VRP cross DOWN +{STRAT.vrp_exit_level} | "
        f"max_hold={STRAT.max_hold_hours}h | SL={STRAT.stop_loss_pct}%\n"
        f"  POLL                 = {STRAT.poll_interval_sec}s\n"
        f"  HL symbol            = {HL.perp_symbol}-PERP\n"
        f"  HL private_key set   = {'YES' if HL.private_key else 'NO'}\n"
    )
