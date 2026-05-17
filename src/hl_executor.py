"""
Hyperliquid executor. Wraps the official Python SDK with safety checks:
  - DRY_RUN mode prints intended actions, doesn't submit
  - MAX_POSITION_USD hard cap
  - Read-only methods always work (positions, account state)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config

log = logging.getLogger(__name__)


@dataclass
class Position:
    coin:        str
    size_eth:    float     # signed: + long, − short
    entry_px:    float
    margin_used: float
    unrealized:  float

    @property
    def is_long(self) -> bool:
        return self.size_eth > 0

    @property
    def is_short(self) -> bool:
        return self.size_eth < 0

    @property
    def size_usd(self) -> float:
        return abs(self.size_eth) * self.entry_px


class HLExecutor:
    """Hyperliquid client. Safe by default."""

    def __init__(self):
        self.cfg = config.HL
        self.risk = config.RISK
        self._info = None
        self._exchange = None
        self._account_address = None
        self._init_client()

    def _init_client(self):
        """Lazy import so requirements stay slim when only signal-side runs."""
        try:
            from hyperliquid.info import Info
            from hyperliquid.exchange import Exchange
            from eth_account import Account
        except ImportError as e:
            log.warning(f"Hyperliquid SDK not installed yet: {e}")
            return

        if not self.cfg.private_key:
            log.warning("HL_PRIVATE_KEY not set — running info-only mode")
            self._info = Info(self.cfg.api_url, skip_ws=True)
            return

        signer_key = self.cfg.api_wallet_key or self.cfg.private_key
        wallet = Account.from_key(signer_key)
        # The "address" we manage is the main wallet — even if signing
        # via API sub-wallet.
        main_addr = Account.from_key(self.cfg.private_key).address
        self._account_address = main_addr

        self._info = Info(self.cfg.api_url, skip_ws=True)
        self._exchange = Exchange(wallet, self.cfg.api_url,
                                   account_address=main_addr)
        log.info(f"HL client initialized for {main_addr}")

    # ─── Read-only ─────────────────────────────────────────────

    def get_mid_price(self) -> float:
        """ETH-PERP mid price."""
        if self._info is None:
            return float("nan")
        mids = self._info.all_mids()
        return float(mids.get(self.cfg.perp_symbol, "nan"))

    def get_position(self) -> Position | None:
        """Current ETH-PERP position, if any."""
        if self._info is None or self._account_address is None:
            return None
        state = self._info.user_state(self._account_address)
        for pos in state.get("assetPositions", []):
            p = pos["position"]
            if p["coin"] != self.cfg.perp_symbol:
                continue
            size = float(p["szi"])
            if abs(size) < 1e-9:
                continue
            return Position(
                coin=p["coin"],
                size_eth=size,
                entry_px=float(p["entryPx"]),
                margin_used=float(p.get("marginUsed", 0)),
                unrealized=float(p.get("unrealizedPnl", 0)),
            )
        return None

    def get_usdc_balance(self) -> float:
        """Free USDC available for trading."""
        if self._info is None or self._account_address is None:
            return 0.0
        state = self._info.user_state(self._account_address)
        return float(state.get("withdrawable", "0"))

    # ─── Order placement ───────────────────────────────────────

    def open_long(self, size_usd: float) -> dict:
        """
        Open a LONG ETH-PERP position with `size_usd` of notional.
        Submitted as a taker market order (IOC). Capped by MAX_POSITION_USD.
        """
        size_usd = min(size_usd, self.risk.max_position_usd)
        mid = self.get_mid_price()
        if mid != mid or mid <= 0:  # nan check
            return {"status": "error", "reason": "could not fetch mid price"}
        size_eth = size_usd / mid

        action = {
            "intent":    "open_long",
            "symbol":    self.cfg.perp_symbol,
            "size_eth":  round(size_eth, 4),
            "size_usd":  size_usd,
            "mid_price": mid,
        }

        if self.risk.dry_run:
            log.info(f"[DRY_RUN] would submit: {action}")
            return {"status": "dry_run", **action}

        if self._exchange is None:
            return {"status": "error", "reason": "no signing key configured"}

        # Real submission — taker market order
        result = self._exchange.market_open(
            name=self.cfg.perp_symbol,
            is_buy=True,
            sz=round(size_eth, 4),
            slippage=0.005,  # 50 bps slippage cap
        )
        log.info(f"submitted LONG: {result}")
        return {"status": "submitted", "result": result, **action}

    def close_position(self) -> dict:
        """Market-close current ETH-PERP position."""
        pos = self.get_position()
        if pos is None:
            return {"status": "noop", "reason": "no open position"}

        action = {
            "intent":   "close_position",
            "symbol":   self.cfg.perp_symbol,
            "size_eth": pos.size_eth,
        }

        if self.risk.dry_run:
            log.info(f"[DRY_RUN] would submit close: {action}")
            return {"status": "dry_run", **action}

        if self._exchange is None:
            return {"status": "error", "reason": "no signing key configured"}

        result = self._exchange.market_close(coin=self.cfg.perp_symbol)
        log.info(f"submitted CLOSE: {result}")
        return {"status": "submitted", "result": result, **action}
