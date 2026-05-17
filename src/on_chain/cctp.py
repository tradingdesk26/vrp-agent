"""
CCTP V2 bridge: USDC between Base and Hyperliquid (HyperEVM → HyperCore).

Deposit flow (Base → HyperCore):
  1. approve(USDC, TokenMessengerV2, amount)
  2. TokenMessengerV2.depositForBurn(
       amount, destDomain=19 (HyperEVM), mintRecipient=padded(EOA),
       burnToken=USDC, destinationCaller=0, maxFee=fee, finality=1000 (Fast))
  3. Poll Circle Iris attestation API (~8s on Base)
  4. With Fast V2, Circle's auto-relayer mints USDC on HyperEVM to our EOA
  5. (next step — automatic via HyperEVM ↔ HyperCore link, or manual via
     CoreDepositWallet.deposit) — handled by separate forward call

Withdrawal flow (HyperCore → Base):
  1. HL Exchange API: withdraw_from_bridge → credits USDC on HyperEVM
  2. depositForBurn on HyperEVM, destDomain=6 (Base), mintRecipient=EOA on Base
  3. Poll attestation
  4. Auto-mint on Base

Iris attestation API:
  GET https://iris-api.circle.com/v2/messages/{srcDomain}?transactionHash={hash}
  Returns: { messages: [{ status, message, attestation, ... }] }
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests
from web3 import Web3

from .. import config
from .client import BaseClient, USDC_BASE, TxResult

log = logging.getLogger(__name__)


# ─── CCTP V2 addresses (Base and HyperEVM both via deterministic CREATE2)
TOKEN_MESSENGER_V2     = Web3.to_checksum_address("0x28b5a0e9C621a5BadaA536219b3a228C8168cf5d")
MESSAGE_TRANSMITTER_V2 = Web3.to_checksum_address("0x81D40F21F12A8F0E3252Bccb954D722d4c464B64")

# Domain IDs
DOMAIN_BASE     = 6
DOMAIN_HYPEREVM = 19

# Finality thresholds
FINALITY_FAST     = 1000   # ≤1000 = Fast (8-30s)
FINALITY_STANDARD = 2000   # 2000 = Standard (~15min on Base)

# Iris attestation endpoint
IRIS_API = "https://iris-api.circle.com/v2/messages"

# Minimal ABI
TOKEN_MESSENGER_V2_ABI = [
    {"inputs": [
        {"name": "amount",                "type": "uint256"},
        {"name": "destinationDomain",     "type": "uint32"},
        {"name": "mintRecipient",         "type": "bytes32"},
        {"name": "burnToken",             "type": "address"},
        {"name": "destinationCaller",     "type": "bytes32"},
        {"name": "maxFee",                "type": "uint256"},
        {"name": "minFinalityThreshold",  "type": "uint32"},
     ],
     "name": "depositForBurn", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
]


@dataclass
class BridgeResult:
    status: str
    direction: str
    amount: int
    burn_tx: str | None = None
    attestation: str | None = None
    error: str | None = None


def address_to_bytes32(addr: str) -> bytes:
    """Left-pad 20-byte address to 32 bytes."""
    return bytes(12) + bytes.fromhex(addr[2:] if addr.startswith("0x") else addr)


class CCTPBridge:
    def __init__(self, client: BaseClient | None = None):
        self.c = client or BaseClient()
        self.tm = self.c.w3.eth.contract(address=TOKEN_MESSENGER_V2,
                                          abi=TOKEN_MESSENGER_V2_ABI)

    def _ensure_approval(self, amount: int):
        cur = self.c.allowance(USDC_BASE, TOKEN_MESSENGER_V2)
        if cur >= amount:
            return
        log.info(f"  approving USDC → TokenMessengerV2 (current allow={cur})")
        r = self.c.approve(USDC_BASE, TOKEN_MESSENGER_V2, 2**256 - 1)
        if r.status == "error":
            raise RuntimeError(f"approve failed: {r.error}")
        # RPC propagation: wait until allowance visible via eth_call.
        # Alchemy's read endpoint lags ~1-2s after confirmation.
        for _ in range(10):
            time.sleep(0.5)
            if self.c.allowance(USDC_BASE, TOKEN_MESSENGER_V2) >= amount:
                return
        log.warning("  allowance not visible after 5s — proceeding anyway")

    def deposit_to_hl(self, amount_usdc_raw: int,
                       fast: bool = True, max_fee_bps: int = 5) -> BridgeResult:
        """
        Burn USDC on Base, attestation → mint on HyperEVM.
        After mint, separate forward needed to push from HyperEVM → HyperCore.

        amount_usdc_raw: raw 6-dec USDC (e.g. 1_000_000 = $1)
        max_fee_bps: max Circle fee in basis points (5 = 0.05%)
        """
        direction = "Base → HyperEVM"
        if config.RISK.dry_run:
            log.info(f"[DRY_RUN] {direction}: {amount_usdc_raw / 1e6:.4f} USDC")
            return BridgeResult(status="dry_run", direction=direction,
                                amount=amount_usdc_raw)

        self._ensure_approval(amount_usdc_raw)

        max_fee = amount_usdc_raw * max_fee_bps // 10_000
        mint_recipient = address_to_bytes32(self.c.address)
        dest_caller    = bytes(32)  # no restriction
        finality       = FINALITY_FAST if fast else FINALITY_STANDARD

        log.info(f"  depositForBurn: amount={amount_usdc_raw}, "
                 f"dest={DOMAIN_HYPEREVM}, recipient={self.c.address}, "
                 f"max_fee={max_fee}, finality={finality}")
        result = self.c._send(
            self.tm.functions.depositForBurn(
                amount_usdc_raw,
                DOMAIN_HYPEREVM,
                mint_recipient,
                USDC_BASE,
                dest_caller,
                max_fee,
                finality,
            ),
            gas_limit=200_000,
        )
        if result.status != "submitted":
            return BridgeResult(status="error", direction=direction,
                                amount=amount_usdc_raw, error=result.error)
        log.info(f"  burn tx: {result.tx_hash}")
        return BridgeResult(
            status="burned",
            direction=direction,
            amount=amount_usdc_raw,
            burn_tx=result.tx_hash,
        )

    def wait_for_attestation(self, burn_tx: str,
                              src_domain: int = DOMAIN_BASE,
                              max_wait_sec: int = 120,
                              poll_interval_sec: int = 3) -> dict | None:
        """Poll Iris API until attestation is `complete`."""
        url = f"{IRIS_API}/{src_domain}?transactionHash={burn_tx}"
        start = time.time()
        while time.time() - start < max_wait_sec:
            try:
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    msgs = data.get("messages", [])
                    if msgs:
                        m = msgs[0]
                        status = m.get("status", "")
                        log.info(f"  attestation status: {status}")
                        if status == "complete":
                            return m
            except Exception as e:
                log.warning(f"  poll error: {e}")
            time.sleep(poll_interval_sec)
        log.error(f"  attestation timeout after {max_wait_sec}s")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    b = CCTPBridge()
    # Dry-run: deposit $1 USDC Base → HyperEVM
    print("Test: deposit $1 USDC Base → HyperEVM (dry-run)")
    res = b.deposit_to_hl(1_000_000)
    print(f"  status:    {res.status}")
    print(f"  direction: {res.direction}")
    print(f"  amount:    ${res.amount/1e6:.4f}")
    if res.burn_tx:
        print(f"  burn tx:   {res.burn_tx}")
    if res.error:
        print(f"  error:     {res.error}")
