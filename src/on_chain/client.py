"""
Web3 client for Base mainnet + ERC20 helpers.

All on-chain operations route through this module so we have a single
place to manage RPC, signer, gas estimation, and tx receipt handling.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError

from .. import config

log = logging.getLogger(__name__)

import os as _os

# ─── Base mainnet constants ─────────────────────────────────────────
# Use private RPC from env if set, otherwise public (rate-limited).
BASE_RPC = _os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
BASE_CHAIN_ID = 8453

# Tokens
USDC_BASE = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
EURC_BASE = Web3.to_checksum_address("0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42")

# Uniswap v4 contracts on Base
POOL_MANAGER = Web3.to_checksum_address("0x498581fF718922c3f8e6A244956aF099B2652b2b")
POSM         = Web3.to_checksum_address("0x7C5f5A4bBd8fD63184577525326123B519429bDc")
PERMIT2      = Web3.to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3")

# Our hooks
HOOK_USDC_EURC = Web3.to_checksum_address("0xFEf708C7879c0d1b9e45D9Eb8dc64C976896c0c8")  # FX
HOOK_ETH_USDC  = Web3.to_checksum_address("0x7fB4846d3987476577319f112731BB04f45880C8")  # round-30

# Our pool config (the live one with user's LP)
POOL_FEE = 0x800000        # DYNAMIC_FEE_FLAG
POOL_TICK_SPACING = 60     # URL pool

# Minimal ERC20 ABI
ERC20_ABI = [
    {"inputs": [{"name": "a", "type": "address"}], "name": "balanceOf",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}],
     "stateMutability": "view", "type": "function"},
]


@dataclass
class TxResult:
    status: str            # "submitted" | "dry_run" | "error"
    tx_hash: str | None = None
    receipt: Any = None
    gas_used: int | None = None
    error: str | None = None


class BaseClient:
    """Single web3 client for all Base mainnet operations."""

    def __init__(self, rpc_url: str = BASE_RPC):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self.w3.is_connected():
            raise RuntimeError(f"Cannot connect to Base RPC {rpc_url}")

        pk = config.HL.private_key  # same key for HL + Base ops
        if not pk:
            log.warning("No private key; client is read-only")
            self.account = None
            self.address = None
            self._nonce = None
        else:
            self.account = Account.from_key(pk)
            self.address = self.account.address
            # Local nonce counter — RPC's get_transaction_count lags ~1-2s
            # after a tx confirms, causing "replacement underpriced" if we
            # submit back-to-back. Refreshed on init + after errors.
            self._nonce = self.w3.eth.get_transaction_count(self.address)
            log.info(f"BaseClient signer: {self.address} (nonce={self._nonce})")

    # ─── ERC20 helpers ────────────────────────────────────────────

    def erc20(self, token: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)

    def balance(self, token: str, owner: str | None = None) -> int:
        """Raw token balance (not decimal-adjusted)."""
        owner = Web3.to_checksum_address(owner or self.address)
        return self.erc20(token).functions.balanceOf(owner).call()

    def allowance(self, token: str, spender: str, owner: str | None = None) -> int:
        owner = Web3.to_checksum_address(owner or self.address)
        return self.erc20(token).functions.allowance(
            owner, Web3.to_checksum_address(spender)
        ).call()

    def approve(self, token: str, spender: str, amount: int) -> TxResult:
        """ERC20 approve. amount=2**256-1 for unlimited."""
        if config.RISK.dry_run:
            log.info(f"[DRY_RUN] approve {token} → {spender}, amount={amount}")
            return TxResult(status="dry_run")
        if self.account is None:
            return TxResult(status="error", error="no signer")
        c = self.erc20(token)
        try:
            return self._send(c.functions.approve(
                Web3.to_checksum_address(spender), amount
            ))
        except Exception as e:
            return TxResult(status="error", error=str(e))

    # ─── Transaction helpers ─────────────────────────────────────

    def _send(self, fn, value: int = 0, gas_limit: int | None = None) -> TxResult:
        if self.account is None:
            return TxResult(status="error", error="no signer")
        try:
            tx = fn.build_transaction({
                "from":     self.address,
                "nonce":    self._nonce,
                "chainId":  BASE_CHAIN_ID,
                "value":    value,
                "maxFeePerGas":         self.w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": int(0.001e9),  # 0.001 gwei tip
            })
            if gas_limit:
                tx["gas"] = gas_limit
            signed = self.account.sign_transaction(tx)
            # web3.py compat: raw_transaction (v7+) vs rawTransaction (v6)
            raw_tx = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            self._nonce += 1   # bump local counter immediately
            tx_hex = tx_hash.hex()
            log.info(f"submitted: {tx_hex} (nonce={self._nonce - 1})")
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            log.info(f"  confirmed: status={receipt.status}, gas={receipt.gasUsed}")
            return TxResult(
                status="submitted" if receipt.status == 1 else "error",
                tx_hash=tx_hex,
                receipt=receipt,
                gas_used=receipt.gasUsed,
                error=None if receipt.status == 1 else "reverted",
            )
        except ContractLogicError as e:
            return TxResult(status="error", error=f"revert: {e}")
        except Exception as e:
            # On any error, re-sync local nonce from chain to recover
            try:
                self._nonce = self.w3.eth.get_transaction_count(self.address)
            except Exception:
                pass
            return TxResult(status="error", error=str(e))

    def call_contract(self, address: str, abi: list, fn_name: str, *args):
        c = self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
        return getattr(c.functions, fn_name)(*args).call()


NATIVE_ETH = Web3.to_checksum_address("0x" + "00" * 20)


def make_pool_key():
    """PoolKey struct for USDC/EURC pool (Base)."""
    return {
        "currency0":   EURC_BASE,
        "currency1":   USDC_BASE,
        "fee":         POOL_FEE,
        "tickSpacing": POOL_TICK_SPACING,
        "hooks":       HOOK_USDC_EURC,
    }


def make_pool_key_eth_usdc():
    """PoolKey struct for ETH/USDC round-30 pool (Base)."""
    return {
        "currency0":   NATIVE_ETH,
        "currency1":   USDC_BASE,
        "fee":         POOL_FEE,
        "tickSpacing": POOL_TICK_SPACING,
        "hooks":       HOOK_ETH_USDC,
    }


def compute_pool_id(pool_key: dict) -> bytes:
    """keccak256(abi.encode(PoolKey)) — canonical v4 pool id."""
    from eth_abi import encode as abi_encode
    encoded = abi_encode(
        ["(address,address,uint24,int24,address)"],
        [(
            pool_key["currency0"], pool_key["currency1"],
            pool_key["fee"], pool_key["tickSpacing"],
            pool_key["hooks"],
        )],
    )
    return Web3.keccak(encoded)


def read_pool_slot0(client, pool_id: bytes) -> dict:
    """Read sqrtPriceX96 + tick via PoolManager.extsload.

    Pools mapping at slot 6. slot for poolId = keccak256(abi.encode(poolId, 6)).
    slot0 packed: lower 160 bits sqrtPriceX96, next 24 bits tick (int24),
    then 24-bit protocolFee, 24-bit lpFee.
    """
    from eth_abi import encode as abi_encode
    POOL_MGR_EXTSLOAD_ABI = [
        {"inputs": [{"name": "slot", "type": "bytes32"}],
         "name": "extsload", "outputs": [{"type": "bytes32"}],
         "stateMutability": "view", "type": "function"},
    ]
    slot_loc = Web3.keccak(abi_encode(["bytes32", "uint256"], [pool_id, 6]))
    raw_bytes = client.call_contract(POOL_MANAGER, POOL_MGR_EXTSLOAD_ABI, "extsload", slot_loc)
    raw_int = int.from_bytes(raw_bytes, "big")
    sqrt_price_x96 = raw_int & ((1 << 160) - 1)
    tick_raw = (raw_int >> 160) & ((1 << 24) - 1)
    tick = tick_raw - (1 << 24) if tick_raw >= (1 << 23) else tick_raw
    return {"sqrt_price_x96": sqrt_price_x96, "tick": tick}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    c = BaseClient()
    print(f"Connected to Base: chainId={c.w3.eth.chain_id}, block={c.w3.eth.block_number}")
    print(f"Signer: {c.address}")
    print(f"USDC balance: {c.balance(USDC_BASE) / 1e6:.4f}")
    print(f"EURC balance: {c.balance(EURC_BASE) / 1e6:.4f}")
    print(f"ETH balance:  {c.w3.eth.get_balance(c.address) / 1e18:.6f}")
