"""
Uniswap v3 ETH ↔ USDC swap on Base via SwapRouter02.

Uses the canonical USDC/WETH 0.05% pool (deep liquidity, low slippage).
Handles native ETH automatically — SwapRouter02 wraps/unwraps WETH9.

For ETH → USDC (Phase 2 forward, after LP burn):
  - We have native ETH balance
  - SwapRouter02.exactInputSingle with tokenIn=WETH9 + msg.value=eth_amount
    (router internally wraps msg.value to WETH9)
  - Output: USDC to recipient

For USDC → ETH (Phase 2 reverse, before LP mint):
  - We have USDC balance
  - approve USDC to SwapRouter02
  - exactInputSingle with tokenIn=USDC, tokenOut=WETH9
  - Multicall pattern: + unwrapWETH9 to get native ETH out

Addresses on Base mainnet:
"""
from __future__ import annotations

import logging
import time

from eth_abi import encode
from web3 import Web3

from .. import config
from .client import BaseClient, USDC_BASE, TxResult

log = logging.getLogger(__name__)

# Base mainnet
SWAP_ROUTER_02 = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
WETH9          = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")

# Pool: USDC/WETH 0.05% on Base, highest TVL
FEE_005 = 500   # 0.05% in v3 units (1e6 = 100%)

# Sqrt price limits — bounds for trade-through
MIN_SQRT_PRICE_PLUS_1 = 4295128740
MAX_SQRT_PRICE_MINUS_1 = 1461446703485210103287273052203988822378723970341


SWAP_ROUTER_02_ABI = [
    # exactInputSingle
    {"inputs": [{
        "components": [
            {"name": "tokenIn",           "type": "address"},
            {"name": "tokenOut",          "type": "address"},
            {"name": "fee",               "type": "uint24"},
            {"name": "recipient",         "type": "address"},
            {"name": "amountIn",          "type": "uint256"},
            {"name": "amountOutMinimum",  "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ], "name": "params", "type": "tuple"
     }],
     "name": "exactInputSingle",
     "outputs": [{"name": "amountOut", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
    # multicall(bytes[] data)
    {"inputs": [{"name": "data", "type": "bytes[]"}],
     "name": "multicall",
     "outputs": [{"name": "results", "type": "bytes[]"}],
     "stateMutability": "payable", "type": "function"},
    # unwrapWETH9
    {"inputs": [{"name": "amountMinimum", "type": "uint256"},
                {"name": "recipient",     "type": "address"}],
     "name": "unwrapWETH9", "outputs": [],
     "stateMutability": "payable", "type": "function"},
    # refundETH
    {"inputs": [], "name": "refundETH", "outputs": [],
     "stateMutability": "payable", "type": "function"},
]


class V3Swap:
    """Uniswap v3 swap for ETH ↔ USDC on Base."""

    def __init__(self, client: BaseClient | None = None):
        self.c = client or BaseClient()
        self.router = self.c.w3.eth.contract(address=SWAP_ROUTER_02,
                                              abi=SWAP_ROUTER_02_ABI)

    def eth_to_usdc(self, eth_wei: int, min_usdc_out: int = 0) -> TxResult:
        """
        Swap native ETH → USDC via SwapRouter02 (auto-wraps msg.value).
        Output USDC delivered to self.c.address.
        """
        if config.RISK.dry_run:
            log.info(f"[DRY_RUN] swap {eth_wei/1e18:.6f} ETH → USDC "
                     f"(min_out={min_usdc_out/1e6:.4f})")
            return TxResult(status="dry_run")

        params = (
            WETH9,             # tokenIn
            USDC_BASE,         # tokenOut
            FEE_005,           # fee
            self.c.address,    # recipient
            eth_wei,           # amountIn
            min_usdc_out,      # amountOutMinimum
            0,                 # sqrtPriceLimitX96 (0 = no limit)
        )
        log.info(f"  swap {eth_wei/1e18:.6f} ETH → USDC "
                 f"(min_out={min_usdc_out/1e6:.4f})")
        return self.c._send(
            self.router.functions.exactInputSingle(params),
            value=eth_wei,
            gas_limit=300_000,
        )

    def usdc_to_eth(self, usdc_amount: int, min_eth_out_wei: int = 0) -> TxResult:
        """
        Swap USDC → native ETH via SwapRouter02 (multicall: swap + unwrap).

        Steps inside multicall:
          1. exactInputSingle(USDC, WETH9, fee, router, usdc_amount, min_out, 0)
             → WETH stays on router
          2. unwrapWETH9(min_out, recipient=self.c.address)
             → native ETH to caller
        """
        if config.RISK.dry_run:
            log.info(f"[DRY_RUN] swap {usdc_amount/1e6:.4f} USDC → ETH "
                     f"(min_out={min_eth_out_wei/1e18:.6f})")
            return TxResult(status="dry_run")

        # Ensure approval
        cur = self.c.allowance(USDC_BASE, SWAP_ROUTER_02)
        if cur < usdc_amount:
            log.info("  approving USDC → SwapRouter02 max")
            r = self.c.approve(USDC_BASE, SWAP_ROUTER_02, 2**256 - 1)
            if r.status == "error":
                return r
            time.sleep(2)  # RPC propagation

        # Build multicall calldata: swap (WETH out to router) + unwrapWETH9
        swap_params = (
            USDC_BASE, WETH9, FEE_005,
            SWAP_ROUTER_02,    # recipient = router (so router holds WETH)
            usdc_amount, min_eth_out_wei, 0,
        )
        swap_call = self.router.encode_abi("exactInputSingle",
                                             args=[swap_params])
        unwrap_call = self.router.encode_abi("unwrapWETH9",
                                               args=[min_eth_out_wei, self.c.address])

        return self.c._send(
            self.router.functions.multicall([
                Web3.to_bytes(hexstr=swap_call),
                Web3.to_bytes(hexstr=unwrap_call),
            ]),
            gas_limit=400_000,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    swap = V3Swap()
    # Tiny test: 0.0001 ETH → USDC (~$0.23)
    print("Test: 0.0001 ETH → USDC")
    res = swap.eth_to_usdc(100_000_000_000_000)  # 1e14 wei = 0.0001 ETH
    print(f"  status: {res.status}")
    if res.tx_hash:
        print(f"  tx:     {res.tx_hash}")
    if res.error:
        print(f"  error:  {res.error}")
