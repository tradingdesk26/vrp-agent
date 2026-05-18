"""
vrp-agent orchestrator — SESSION-TIMING strategy.

Strategy:
  Every UTC day, open long ETH at 20:00 UTC, close at 22:00 UTC (taker).
  Stop-loss at -5% drawdown.

  Between sessions, capital sits based on VRP zone:
    VRP ≤ 0  →  CASH_ON_HL (USDC on Hyperliquid)
    VRP > 0  →  PARKED_IN_LP (Base ETH/USDC LP)

  When VRP crosses above +30, override session timing and hold persistent
  long until VRP drops below +6 (hysteresis exit). Re-enters session loop
  after that.

  Cross-zero defensive: VRP drops below 0 while in LP → exit LP to
  DEFENSIVE_CASH (USDC on Base). VRP recovers > 0 → re-mint LP.

See `session_strategy.decide()` for the full decision tree.

Run:
  cd vrp-agent
  python -m src.main                # respects DRY_RUN env (default true)
  DRY_RUN=false python -m src.main  # LIVE trading
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime

from . import config
from . import deribit_feed
from . import phase_defensive
from . import phase_session
from . import session_strategy as ss
from . import signal_engine as sig
from . import state_machine as sm
from . import transitions
from .hl_executor import HLExecutor
from .on_chain.client import BaseClient
from .on_chain.lp_manager import LPManager
from .pnl_tracker import PnLTracker

log = logging.getLogger("vrp-agent")


def setup_logging():
    log_file = config.LOG_DIR / f"agent-{datetime.utcnow():%Y-%m-%d}.log"
    logging.basicConfig(
        level=getattr(logging, getattr(config, "LOG_LEVEL", "INFO"), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )


def run():
    setup_logging()
    log.info("=" * 60)
    log.info("vrp-agent SESSION-TIMING strategy starting")
    log.info("=" * 60)
    log.info(config.banner())
    log.info(f"  Session entry hour:    {ss.SESSION_ENTRY_HOUR}:00 UTC")
    log.info(f"  Session exit hour:     {ss.SESSION_EXIT_HOUR}:00 UTC")
    log.info(f"  Stop-loss:             {ss.STOP_LOSS_PCT*100:.1f}%")
    log.info(f"  VRP persistent entry:  +{ss.VRP_PERSISTENT_ENTRY}")
    log.info(f"  VRP persistent exit:   +{ss.VRP_PERSISTENT_EXIT}")

    # Initialize modules
    tracker  = PnLTracker()
    hl_exec  = HLExecutor()
    base     = BaseClient()
    lp_mgr   = LPManager(base)

    # Initial reconciliation
    snap = transitions.snapshot(base, lp_mgr, hl_exec, tracker)
    state = sm.reconcile(snap)
    log.info(f"Initial reconcile: state={state.value}")
    log.info(f"  {snap.fmt()}")

    last_vrp: float | None = None
    last_action_key: str | None = None

    while True:
        try:
            now = datetime.utcnow()
            hour_utc = now.hour
            today_str = now.strftime("%Y-%m-%d")

            # ─── 1. Pull fresh signal ───────────────────────
            df = deribit_feed.fetch_combined(
                "ETH", hours=config.DERIB.history_hours
            )
            sig_snap = sig.compute_snapshot(df)

            # ─── 2. Reconcile on-chain state ────────────────
            chain_snap = transitions.snapshot(base, lp_mgr, hl_exec, tracker)
            chain_state = sm.reconcile(chain_snap)
            if chain_state != state:
                log.info(f"  state changed: {state.value} → {chain_state.value}")
                state = chain_state

            # ─── 2b. Auto-recovery for stuck-in-transit states ─
            # If reconcile detected funds stranded on HyperEVM between Base
            # and HL legs (CCTP poll window expired before relayer delivered),
            # push them through the next leg automatically. Idempotent.
            if state == sm.State.IN_TRANSIT_TO_HL:
                log.warning(f"  ⚠ stuck IN_TRANSIT_TO_HL — auto-pushing to HL")
                try:
                    phase_session._resume_deposit_to_hl()
                    # Re-snapshot to pick up new state on next tick
                    chain_snap = transitions.snapshot(base, lp_mgr, hl_exec, tracker)
                    chain_state = sm.reconcile(chain_snap)
                    log.info(f"    post-recovery state: {chain_state.value}")
                    state = chain_state
                except Exception:
                    log.exception("  ✗ auto-recover IN_TRANSIT_TO_HL failed")
                    time.sleep(60); continue
            elif state == sm.State.IN_TRANSIT_TO_BASE:
                log.warning(f"  ⚠ stuck IN_TRANSIT_TO_BASE — manual recovery needed")
                log.warning(f"    HyperEVM USDC: ${chain_snap.hyperevm_usdc/1e6:.4f}, "
                            f"Base USDC: ${chain_snap.base_usdc/1e6:.4f}")
                log.warning(f"    not auto-recovering (CCTP burn HyperEVM→Base needs "
                            f"phase2_reverse from Step 4); will retry next tick")
                time.sleep(config.STRAT.poll_interval_sec); continue

            # ─── 3. Position context (for stop-loss + entry_mode) ───
            pnl_pct: float | None = None
            entry_mode: str | None = None
            open_trade_id: int | None = None
            if state == sm.State.LONG_ON_HL:
                open_trade = tracker.get_open_trade_full()
                if open_trade:
                    open_trade_id = open_trade["id"]
                    entry_mode = open_trade.get("entry_mode") or "session"
                    if open_trade["entry_price"]:
                        pnl_pct = (sig_snap.price - open_trade["entry_price"]) \
                                   / open_trade["entry_price"]

            # ─── 4. today_session_done? ─────────────────────
            last_session = tracker.last_session_date()
            today_session_done = (last_session == today_str)

            # ─── 5. Decide ──────────────────────────────────
            action = ss.decide(
                state=state,
                vrp_now=sig_snap.vrp,
                vrp_prev=last_vrp,
                hour_utc=hour_utc,
                today_session_done=today_session_done,
                entry_mode=entry_mode,
                pnl_pct=pnl_pct,
            )
            last_vrp = sig_snap.vrp

            # ─── 6. Daily loss-limit gate ───────────────────
            daily_pnl = tracker.daily_loss()
            if daily_pnl <= -config.RISK.daily_loss_limit_usd:
                log.warning(f"DAILY LOSS LIMIT: ${daily_pnl:.2f} ≤ "
                             f"${-config.RISK.daily_loss_limit_usd:.2f}")
                action = ss.Action(ss.Decision.HOLD, "daily_loss_limit")

            # Log heartbeat: on action change OR every 30 min
            sig_key = f"{state.value}/{action.decision.value}/{entry_mode or '-'}"
            heartbeat = (last_action_key != sig_key) or \
                        ((int(time.time()) % 1800) < config.STRAT.poll_interval_sec)
            if heartbeat:
                pnl_str = f" pnl={pnl_pct*100:+.2f}%" if pnl_pct is not None else ""
                log.info(f"{sig_snap.fmt()}  state={state.value}  "
                         f"H={hour_utc:02d}{pnl_str}  "
                         f"→ {action.decision.value}: {action.reason}")
            last_action_key = sig_key
            tracker.log_state(sig_snap, state.value,
                               action.decision.value, action.reason)

            # ─── 7. Execute action ──────────────────────────
            d = action.decision

            if d == ss.Decision.HOLD:
                pass

            elif d == ss.Decision.ENTER_SESSION_LONG:
                log.info(f"  ▶ ENTER_SESSION_LONG ({action.reason})")
                phase_session.enter_long(
                    state=state, mode="session",
                    session_date=today_str,
                    entry_reason=f"session_20h_from_{state.value}",
                )

            elif d == ss.Decision.ENTER_PERSISTENT_LONG:
                log.info(f"  ▶ ENTER_PERSISTENT_LONG ({action.reason})")
                phase_session.enter_long(
                    state=state, mode="persistent",
                    session_date=None,
                    entry_reason=f"persistent_vrp30_from_{state.value}",
                )

            elif d == ss.Decision.UPGRADE_TO_PERSISTENT:
                if open_trade_id:
                    log.info(f"  ▶ UPGRADE_TO_PERSISTENT (trade #{open_trade_id})")
                    tracker.upgrade_to_persistent(open_trade_id)

            elif d == ss.Decision.EXIT_LONG:
                target = action.post_route or "cash"
                log.info(f"  ▶ EXIT_LONG → {target} ({action.reason})")
                phase_session.exit_long(target=target, exit_reason=action.reason)

            elif d == ss.Decision.MOVE_LP_TO_DEFENSIVE:
                log.info(f"  ▶ MOVE_LP_TO_DEFENSIVE ({action.reason})")
                try:
                    phase_defensive.run_to_defensive()
                except Exception:
                    log.exception("  ✗ run_to_defensive FAILED")
                    time.sleep(60)

            elif d == ss.Decision.MOVE_DEFENSIVE_TO_LP:
                log.info(f"  ▶ MOVE_DEFENSIVE_TO_LP ({action.reason})")
                try:
                    phase_defensive.run_to_lp()
                except Exception:
                    log.exception("  ✗ run_to_lp FAILED")
                    time.sleep(60)

            # ─── 8. Sleep ───────────────────────────────────
            time.sleep(config.STRAT.poll_interval_sec)

        except KeyboardInterrupt:
            log.info("interrupted by user, exiting")
            break
        except Exception:
            log.exception("loop iteration failed, retrying after sleep")
            time.sleep(config.STRAT.poll_interval_sec)


if __name__ == "__main__":
    run()
