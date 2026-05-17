"""
Swap module: USDC ↔ EURC via PoolSwapTest router on our own pool.

PoolSwapTest is a v4-core reference router (already deployed by us at
0x252aECA194843310B83F3426cD4E4a7622aba166 on Base). It's the simplest
way to execute single-pool swaps without Universal Router complexity.

Both tokens are ERC-20 (no native ETH), so caller must approve PoolSwapTest
to pull the input token.

Workflow:
  1. approve(input_token, ROUTER, amount)   — one-time / per-amount
  2. router.swap(key, params, settings, "") — actual swap
  3. Output token credited to msg.sender via take()
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from eth_abi import encode
from web3 import Web3

from .. import config
from .client import (
    BaseClient, EURC_BASE, USDC_BASE, HOOK_USDC_EURC,
    POOL_FEE, POOL_TICK_SPACING, make_pool_key, TxResult,
)

log = logging.getLogger(__name__)

POOL_SWAP_TEST = Web3.to_checksum_address("0x252aECA194843310B83F3426cD4E4a7622aba166")

# v4 tick boundaries (min/max sqrt prices ±1 to avoid TickMath bounds)
MIN_SQRT_PRICE_PLUS_1 = 4295128740      # MIN_SQRT_RATIO + 1
MAX_SQRT_PRICE_MINUS_1 = 1461446703485210103287273052203988822378723970341  # MAX − 1

POOL_SWAP_TEST_ABI = [
    {"inputs": [
        {"components": [
            {"name": "currency0",   "type": "address"},
            {"name": "currency1",   "type": "address"},
            {"name": "fee",         "type": "uint24"},
            {"name": "tickSpacing", "type": "int24"},
            {"name": "hooks",       "type": "address"},
         ], "name": "key", "type": "tuple"},
        {"components": [
            {"name": "zeroForOne",        "type": "bool"},
            {"name": "amountSpecified",   "type": "int256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
         ], "name": "params", "type": "tuple"},
        {"components": [
            {"name": "takeClaims",      "type": "bool"},
            {"name": "settleUsingBurn", "type": "bool"},
         ], "name": "testSettings", "type": "tuple"},
        {"name": "hookData", "type": "bytes"},
     ],
     "name": "swap",
     "outputs": [{"name": "delta", "type": "int256"}],
     "stateMutability": "payable", "type": "function"},
]


@dataclass
class SwapResult:
    status: str
    direction: str   # "USDC→EURC" | "EURC→USDC"
    amount_in: int   # raw 6-dec
    amount_out_estimate: int | None = None
    tx_hash: str | None = None
    error: str | None = None


class SwapManager:
    """USDC ↔ EURC swap via our own ARMSHookV3FX pool."""

    def __init__(self, client: BaseClient | None = None):
        self.c = client or BaseClient()
        self.router = self.c.w3.eth.contract(address=POOL_SWAP_TEST,
                                              abi=POOL_SWAP_TEST_ABI)

    def _ensure_approval(self, token: str, amount: int):
        """Approve PoolSwapTest to pull `amount` of `token`."""
        cur = self.c.allowance(token, POOL_SWAP_TEST)
        if cur >= amount:
            log.info(f"  approval OK ({cur} >= {amount})")
            return
        log.info(f"  approving {token} → {POOL_SWAP_TEST}: max")
        r = self.c.approve(token, POOL_SWAP_TEST, 2**256 - 1)
        if r.status == "error":
            raise RuntimeError(f"approve failed: {r.error}")

    def usdc_to_eurc(self, amount_usdc_raw: int) -> SwapResult:
        """Swap exact-in `amount_usdc_raw` USDC → EURC."""
        return self._swap(zero_for_one=False, amount_in=amount_usdc_raw,
                           direction="USDC→EURC", input_token=USDC_BASE)

    def eurc_to_usdc(self, amount_eurc_raw: int) -> SwapResult:
        """Swap exact-in `amount_eurc_raw` EURC → USDC."""
        return self._swap(zero_for_one=True, amount_in=amount_eurc_raw,
                           direction="EURC→USDC", input_token=EURC_BASE)

    def _swap(self, zero_for_one: bool, amount_in: int,
               direction: str, input_token: str) -> SwapResult:
        """Internal: execute single-pool swap.

        zeroForOne=True  → swap currency0 (EURC) for currency1 (USDC)
        zeroForOne=False → swap currency1 (USDC) for currency0 (EURC)
        """
        pk = make_pool_key()

        if config.RISK.dry_run:
            log.info(f"[DRY_RUN] swap {direction}: {amount_in} raw")
            return SwapResult(status="dry_run", direction=direction,
                              amount_in=amount_in)

        self._ensure_approval(input_token, amount_in)

        # Exact-input: amountSpecified is negative
        pk_tuple = (pk["currency0"], pk["currency1"], pk["fee"],
                    pk["tickSpacing"], pk["hooks"])
        params_tuple = (
            zero_for_one,
            -amount_in,
            MIN_SQRT_PRICE_PLUS_1 if zero_for_one else MAX_SQRT_PRICE_MINUS_1,
        )
        settings_tuple = (False, False)  # takeClaims=False, settleUsingBurn=False
        hook_data = b""

        log.info(f"  swap {direction}, amount_in={amount_in} raw")
        result = self.c._send(
            self.router.functions.swap(pk_tuple, params_tuple, settings_tuple, hook_data),
            gas_limit=400_000,
        )
        return SwapResult(
            status=result.status,
            direction=direction,
            amount_in=amount_in,
            tx_hash=result.tx_hash,
            error=result.error,
        )


# ─── Standalone test (dry-run unless DRY_RUN=false) ────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    sm = SwapManager()
    # tiny test: 0.001 USDC → EURC
    print("Test: 0.001 USDC → EURC")
    res = sm.usdc_to_eurc(1000)  # 1000 raw = 0.001 USDC
    print(f"  status:    {res.status}")
    print(f"  direction: {res.direction}")
    if res.tx_hash:
        print(f"  tx:        {res.tx_hash}")
    if res.error:
        print(f"  error:     {res.error}")
