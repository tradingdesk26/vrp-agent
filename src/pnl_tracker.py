"""
SQLite-backed PnL tracker.

Tables:
  trades       — open/close events, entry/exit prices, PnL
  state_log    — periodic snapshots of agent state for debugging
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_ts     TEXT,
    entry_price  REAL,
    entry_size   REAL,
    entry_reason TEXT,
    entry_mode   TEXT,            -- 'session' | 'persistent'
    session_date TEXT,            -- 'YYYY-MM-DD' UTC, only for session-mode entries
    exit_ts      TEXT,
    exit_price   REAL,
    exit_reason  TEXT,
    pnl_usd      REAL,
    pnl_pct      REAL,
    dry_run      INTEGER,
    raw_open     TEXT,
    raw_close    TEXT
);

CREATE TABLE IF NOT EXISTS state_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT,
    state     TEXT,
    price     REAL,
    r_short   REAL,
    r_long    REAL,
    dvol      REAL,
    vrp       REAL,
    vrp_peak  REAL,
    decision  TEXT,
    decision_reason TEXT
);

CREATE TABLE IF NOT EXISTS lp_positions (
    token_id      INTEGER PRIMARY KEY,
    pool_label    TEXT,           -- 'ETH/USDC' | 'USDC/EURC' | ...
    hook          TEXT,
    tick_lower    INTEGER,
    tick_upper    INTEGER,
    minted_ts     TEXT,
    minted_block  INTEGER,
    mint_tx       TEXT,
    initial_amount0  TEXT,        -- raw bigint as string
    initial_amount1  TEXT,
    burnt_ts      TEXT,           -- NULL while active
    burnt_tx      TEXT
);

CREATE INDEX IF NOT EXISTS idx_state_ts ON state_log(ts);
CREATE INDEX IF NOT EXISTS idx_trades_entry ON trades(entry_ts);
CREATE INDEX IF NOT EXISTS idx_lp_active ON lp_positions(burnt_ts);
"""


class PnLTracker:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or (config.DATA_DIR / "state.sqlite")
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self):
        """Add columns added after initial schema (idempotent)."""
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(trades)").fetchall()}
        if "entry_mode" not in cols:
            self._conn.execute("ALTER TABLE trades ADD COLUMN entry_mode TEXT")
        if "session_date" not in cols:
            self._conn.execute("ALTER TABLE trades ADD COLUMN session_date TEXT")

    # ─── Trade lifecycle ───────────────────────────────────────

    def open_trade(self, ts: str, price: float, size_eth: float,
                    reason: str, dry_run: bool, raw: dict,
                    entry_mode: str | None = None,
                    session_date: str | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO trades(entry_ts, entry_price, entry_size, "
            "entry_reason, dry_run, raw_open, entry_mode, session_date) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, price, size_eth, reason, int(dry_run), json.dumps(raw),
             entry_mode, session_date),
        )
        self._conn.commit()
        return cur.lastrowid

    def upgrade_to_persistent(self, trade_id: int):
        """Mark a session-mode trade as upgraded to persistent."""
        self._conn.execute(
            "UPDATE trades SET entry_mode='persistent' WHERE id=?",
            (trade_id,),
        )
        self._conn.commit()

    def get_open_trade_full(self) -> dict | None:
        """Same as get_open_trade but includes entry_mode + session_date."""
        row = self._conn.execute(
            "SELECT id, entry_ts, entry_price, entry_size, entry_reason, "
            "entry_mode, session_date "
            "FROM trades WHERE exit_ts IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "entry_ts": row[1], "entry_price": row[2],
            "entry_size": row[3], "entry_reason": row[4],
            "entry_mode": row[5], "session_date": row[6],
        }

    def last_session_date(self) -> str | None:
        """Most recent session_date among all trades (open or closed)."""
        row = self._conn.execute(
            "SELECT MAX(session_date) FROM trades WHERE session_date IS NOT NULL"
        ).fetchone()
        return row[0] if row and row[0] else None

    def last_meaningful_state(self, exclude: set[str] | None = None) -> str | None:
        """Most recent state in state_log excluding stuck/unknown states.

        Used at agent startup to disambiguate IN_TRANSIT_* states — we
        check what the agent was doing BEFORE it got stuck (e.g.,
        LONG_ON_HL → reverse stalled; PARKED_IN_LP → forward stalled).
        """
        exclude = exclude or set()
        if not exclude:
            row = self._conn.execute(
                "SELECT state FROM state_log ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        # NOT IN clause with parameter binding
        placeholders = ",".join(["?"] * len(exclude))
        row = self._conn.execute(
            f"SELECT state FROM state_log WHERE state NOT IN ({placeholders}) "
            f"ORDER BY ts DESC LIMIT 1",
            tuple(exclude),
        ).fetchone()
        return row[0] if row else None

    def close_trade(self, trade_id: int, ts: str, price: float,
                     reason: str, raw: dict):
        row = self._conn.execute(
            "SELECT entry_price, entry_size FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if not row:
            return
        entry_price, entry_size = row
        pnl_usd = (price - entry_price) * entry_size
        pnl_pct = (price - entry_price) / entry_price * 100 if entry_price else 0
        self._conn.execute(
            "UPDATE trades SET exit_ts=?, exit_price=?, exit_reason=?, "
            "pnl_usd=?, pnl_pct=?, raw_close=? WHERE id=?",
            (ts, price, reason, pnl_usd, pnl_pct, json.dumps(raw), trade_id),
        )
        self._conn.commit()

    def open_trade_id(self) -> int | None:
        """ID of currently-open trade, if any."""
        row = self._conn.execute(
            "SELECT id FROM trades WHERE exit_ts IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def get_open_trade(self) -> dict | None:
        row = self._conn.execute(
            "SELECT id, entry_ts, entry_price, entry_size, entry_reason "
            "FROM trades WHERE exit_ts IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "entry_ts": row[1], "entry_price": row[2],
            "entry_size": row[3], "entry_reason": row[4],
        }

    # ─── State logging ─────────────────────────────────────────

    def log_state(self, snap, state: str, decision: str, reason: str):
        # vrp_peak column kept for backwards compat — write NULL with
        # level-based strategy (no peak tracked).
        self._conn.execute(
            "INSERT INTO state_log(ts, state, price, r_short, r_long, "
            "dvol, vrp, vrp_peak, decision, decision_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                snap.timestamp.isoformat(),
                state,
                snap.price,
                snap.r_short,
                snap.r_long,
                snap.dvol,
                snap.vrp,
                None,
                decision,
                reason,
            ),
        )
        self._conn.commit()

    # ─── Aggregates ────────────────────────────────────────────

    def summary(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*), SUM(pnl_usd), AVG(pnl_pct), "
            "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) "
            "FROM trades WHERE exit_ts IS NOT NULL"
        ).fetchone()
        n, total_usd, avg_pct, wins = row
        return {
            "closed_trades":   n or 0,
            "total_pnl_usd":   float(total_usd or 0),
            "avg_pnl_pct":     float(avg_pct or 0),
            "wins":            int(wins or 0),
            "win_rate":        (wins / n) if n else 0.0,
        }

    # ─── LP position lifecycle ─────────────────────────────────

    def record_lp_mint(
        self, token_id: int, pool_label: str, hook: str,
        tick_lower: int, tick_upper: int,
        ts: str, block: int, tx: str,
        initial_amount0: int, initial_amount1: int,
    ):
        self._conn.execute(
            "INSERT OR REPLACE INTO lp_positions("
            " token_id, pool_label, hook, tick_lower, tick_upper,"
            " minted_ts, minted_block, mint_tx,"
            " initial_amount0, initial_amount1) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (token_id, pool_label, hook, tick_lower, tick_upper,
             ts, block, tx, str(initial_amount0), str(initial_amount1)),
        )
        self._conn.commit()

    def record_lp_burn(self, token_id: int, ts: str, tx: str):
        self._conn.execute(
            "UPDATE lp_positions SET burnt_ts=?, burnt_tx=? WHERE token_id=?",
            (ts, tx, token_id),
        )
        self._conn.commit()

    def active_lp(self) -> dict | None:
        """Most recent active (un-burnt) LP NFT. None if no active position."""
        row = self._conn.execute(
            "SELECT token_id, pool_label, hook, tick_lower, tick_upper, "
            "minted_ts, mint_tx, initial_amount0, initial_amount1 "
            "FROM lp_positions WHERE burnt_ts IS NULL "
            "ORDER BY token_id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "token_id":         row[0],
            "pool_label":       row[1],
            "hook":             row[2],
            "tick_lower":       row[3],
            "tick_upper":       row[4],
            "minted_ts":        row[5],
            "mint_tx":          row[6],
            "initial_amount0":  int(row[7]),
            "initial_amount1":  int(row[8]),
        }

    def daily_loss(self) -> float:
        """Sum of PnL for trades closed today (UTC)."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        row = self._conn.execute(
            "SELECT SUM(pnl_usd) FROM trades "
            "WHERE exit_ts IS NOT NULL AND substr(exit_ts,1,10)=?",
            (today,),
        ).fetchone()
        return float(row[0] or 0)
