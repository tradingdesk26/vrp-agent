"""
LP manager: wraps Uniswap v4 PositionManager (POSM) for our USDC/EURC pool.

Operations:
  - read_position(tokenId) — owner, liquidity, ticks, PoolKey
  - mint_position(...)    — create new full-range LP NFT
  - burn_position(tokenId) — close + receive both tokens back

POSM on Base: 0x7C5f5A4bBd8fD63184577525326123B519429bDc
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from eth_abi import encode
from web3 import Web3

from .. import config
from .client import (
    BaseClient, POSM, EURC_BASE, USDC_BASE, HOOK_USDC_EURC, PERMIT2,
    POOL_FEE, POOL_TICK_SPACING, make_pool_key, TxResult,
)

# Permit2 ABI subset
PERMIT2_ABI = [
    {"inputs": [
        {"name": "token",      "type": "address"},
        {"name": "spender",    "type": "address"},
        {"name": "amount",     "type": "uint160"},
        {"name": "expiration", "type": "uint48"},
     ], "name": "approve", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [
        {"name": "user",    "type": "address"},
        {"name": "token",   "type": "address"},
        {"name": "spender", "type": "address"},
     ], "name": "allowance",
     "outputs": [
        {"name": "amount",     "type": "uint160"},
        {"name": "expiration", "type": "uint48"},
        {"name": "nonce",      "type": "uint48"},
     ],
     "stateMutability": "view", "type": "function"},
]

PERMIT2_MAX_AMOUNT = (1 << 160) - 1
PERMIT2_MAX_EXPIRATION = (1 << 48) - 1

log = logging.getLogger(__name__)


# v4 Actions enum (from v4-periphery/src/libraries/Actions.sol)
ACTION_INCREASE_LIQUIDITY = 0x00
ACTION_DECREASE_LIQUIDITY = 0x01
ACTION_MINT_POSITION      = 0x02
ACTION_BURN_POSITION      = 0x03
ACTION_SETTLE_PAIR        = 0x0d
ACTION_TAKE_PAIR          = 0x11

# POSM read-only ABI subset
POSM_ABI = [
    {"inputs": [{"name": "tokenId", "type": "uint256"}],
     "name": "ownerOf",
     "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}],
     "name": "getPositionLiquidity",
     "outputs": [{"type": "uint128"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}],
     "name": "getPoolAndPositionInfo",
     "outputs": [
        {"components": [
            {"name": "currency0",   "type": "address"},
            {"name": "currency1",   "type": "address"},
            {"name": "fee",         "type": "uint24"},
            {"name": "tickSpacing", "type": "int24"},
            {"name": "hooks",       "type": "address"},
         ], "name": "poolKey", "type": "tuple"},
        {"name": "info", "type": "uint256"},
     ],
     "stateMutability": "view", "type": "function"},
    {"inputs": [
        {"name": "unlockData", "type": "bytes"},
        {"name": "deadline",   "type": "uint256"},
     ],
     "name": "modifyLiquidities",
     "outputs": [], "stateMutability": "payable", "type": "function"},
]


@dataclass
class PositionInfo:
    tokenId:     int
    owner:       str | None
    exists:      bool
    liquidity:   int
    tick_lower:  int | None
    tick_upper:  int | None
    pool_key:    dict | None


def _decode_position_info(info: int) -> tuple[int, int]:
    """Unpack tickLower (signed int24) and tickUpper from packed uint256.
    See PositionInfoLibrary: TICK_LOWER_OFFSET=8, TICK_UPPER_OFFSET=32."""
    def signextend_24(x: int) -> int:
        x &= 0xFFFFFF
        if x & 0x800000:
            x -= 0x1000000
        return x
    tick_lower = signextend_24(info >> 8)
    tick_upper = signextend_24(info >> 32)
    return tick_lower, tick_upper


class LPManager:
    def __init__(self, client: BaseClient | None = None):
        self.c = client or BaseClient()
        self.posm = self.c.w3.eth.contract(address=POSM, abi=POSM_ABI)
        self.permit2 = self.c.w3.eth.contract(address=PERMIT2, abi=PERMIT2_ABI)

    def setup_approvals(self, tokens: list[str]) -> list[TxResult]:
        """
        Two-stage Permit2 approval flow:
          1. ERC20.approve(PERMIT2, max) — one-time per token
          2. Permit2.approve(POSM, token, max, far_future) — per use, but
             we set max so it's effectively one-time
        Idempotent: skips if already approved.
        """
        results = []
        for token in tokens:
            # Stage 1: ERC20 → Permit2
            cur = self.c.allowance(token, PERMIT2)
            if cur < (1 << 200):
                log.info(f"  ERC20.approve({token} → Permit2, max)")
                r = self.c.approve(token, PERMIT2, 2**256 - 1)
                results.append(r)
                if r.status == "error":
                    log.error(f"    failed: {r.error}")
                    return results
            else:
                log.info(f"  ERC20→Permit2 already approved ({token})")

            # Stage 2: Permit2 → POSM
            try:
                p_amount, p_exp, _nonce = self.permit2.functions.allowance(
                    self.c.address, token, POSM
                ).call()
                if p_amount >= PERMIT2_MAX_AMOUNT // 2 and p_exp > int(time.time()) + 86400:
                    log.info(f"  Permit2→POSM already approved ({token})")
                    continue
            except Exception:
                pass
            log.info(f"  Permit2.approve(POSM ← {token}, max)")
            if config.RISK.dry_run:
                log.info("    [DRY_RUN]")
                results.append(TxResult(status="dry_run"))
                continue
            r = self.c._send(
                self.permit2.functions.approve(
                    Web3.to_checksum_address(token), POSM,
                    PERMIT2_MAX_AMOUNT, PERMIT2_MAX_EXPIRATION,
                ),
                gas_limit=100_000,
            )
            results.append(r)
            if r.status == "error":
                log.error(f"    failed: {r.error}")
                return results
        return results

    # ─── READ ──────────────────────────────────────────────────────

    def read_position(self, tokenId: int) -> PositionInfo:
        # ownerOf may revert if burned — guard it
        owner = None
        exists = False
        try:
            owner = self.posm.functions.ownerOf(tokenId).call()
            exists = True
        except Exception:
            return PositionInfo(tokenId=tokenId, owner=None, exists=False,
                                 liquidity=0, tick_lower=None, tick_upper=None,
                                 pool_key=None)

        liquidity = self.posm.functions.getPositionLiquidity(tokenId).call()
        pool_key_tuple, info = self.posm.functions.getPoolAndPositionInfo(tokenId).call()
        # pool_key_tuple is a (address, address, uint24, int24, address) tuple
        pool_key = {
            "currency0":   pool_key_tuple[0],
            "currency1":   pool_key_tuple[1],
            "fee":         pool_key_tuple[2],
            "tickSpacing": pool_key_tuple[3],
            "hooks":       pool_key_tuple[4],
        }
        tick_lower, tick_upper = _decode_position_info(info)

        return PositionInfo(
            tokenId=tokenId,
            owner=owner,
            exists=exists,
            liquidity=liquidity,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            pool_key=pool_key,
        )

    # ─── WRITE ─────────────────────────────────────────────────────

    def burn(self, tokenId: int,
              amount0_min: int = 0, amount1_min: int = 0,
              deadline_sec: int = 600) -> TxResult:
        """
        Burn LP NFT — closes position to zero liquidity, returns both
        currencies to signer.
        """
        if config.RISK.dry_run:
            log.info(f"[DRY_RUN] burn tokenId={tokenId}")
            return TxResult(status="dry_run")

        # Build PoolKey from current position
        pos = self.read_position(tokenId)
        if not pos.exists:
            return TxResult(status="error", error=f"tokenId {tokenId} burned/missing")
        pk = pos.pool_key
        actions = bytes([ACTION_BURN_POSITION, ACTION_TAKE_PAIR])
        # BURN_POSITION(uint256 tokenId, uint128 amount0Min, uint128 amount1Min, bytes hookData)
        burn_params = encode(
            ["uint256", "uint128", "uint128", "bytes"],
            [tokenId, amount0_min, amount1_min, b""],
        )
        # TAKE_PAIR(Currency currency0, Currency currency1, address recipient)
        take_params = encode(
            ["address", "address", "address"],
            [pk["currency0"], pk["currency1"], self.c.address],
        )
        unlock_data = encode(["bytes", "bytes[]"], [actions, [burn_params, take_params]])
        deadline = int(time.time()) + deadline_sec
        return self.c._send(
            self.posm.functions.modifyLiquidities(unlock_data, deadline),
            gas_limit=400_000,
        )

    def mint(self,
              pool_key: dict,
              tick_lower: int,
              tick_upper: int,
              liquidity: int,
              amount0_max: int,
              amount1_max: int,
              eth_value_wei: int = 0,
              deadline_sec: int = 600) -> TxResult:
        """
        Mint LP NFT in any v4 pool. For pools with native ETH (currency0=0x0)
        pass eth_value_wei = amount0_max so it gets forwarded as msg.value.
        """
        if config.RISK.dry_run:
            log.info(f"[DRY_RUN] mint pool={pool_key['hooks'][:10]}... "
                     f"ticks=[{tick_lower},{tick_upper}] L={liquidity} "
                     f"max0={amount0_max} max1={amount1_max} value={eth_value_wei}")
            return TxResult(status="dry_run")

        actions = bytes([ACTION_MINT_POSITION, ACTION_SETTLE_PAIR])
        pk_tuple = (pool_key["currency0"], pool_key["currency1"],
                    pool_key["fee"], pool_key["tickSpacing"], pool_key["hooks"])
        mint_params = encode(
            ["(address,address,uint24,int24,address)", "int24", "int24",
             "uint256", "uint128", "uint128", "address", "bytes"],
            [pk_tuple, tick_lower, tick_upper, liquidity,
             amount0_max, amount1_max, self.c.address, b""],
        )
        settle_params = encode(
            ["address", "address"],
            [pool_key["currency0"], pool_key["currency1"]],
        )
        unlock_data = encode(["bytes", "bytes[]"],
                              [actions, [mint_params, settle_params]])
        deadline = int(time.time()) + deadline_sec
        return self.c._send(
            self.posm.functions.modifyLiquidities(unlock_data, deadline),
            value=eth_value_wei,
            gas_limit=600_000,
        )


# ─── Standalone test ────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    lp = LPManager()

    # Test against user's existing LP NFT (tokenId 2317884 in USDC/EURC pool)
    test_id = int(sys.argv[1]) if len(sys.argv) > 1 else 2317884
    pos = lp.read_position(test_id)
    print(f"\n=== POSM read_position({test_id}) ===")
    print(f"  exists:    {pos.exists}")
    if pos.exists:
        print(f"  owner:     {pos.owner}")
        print(f"  liquidity: {pos.liquidity}")
        print(f"  ticks:     [{pos.tick_lower}, {pos.tick_upper}]")
        print(f"  pool_key:")
        for k, v in pos.pool_key.items():
            print(f"    {k}: {v}")
