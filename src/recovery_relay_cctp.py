"""
One-shot: manually relay a CCTP V2 burn message to HyperEVM by calling
MessageTransmitter.receiveMessage(message, attestation).

Used when Circle's auto-relayer is slow/down for the HyperEVM lane.

Usage:
  python3 -m src.recovery_relay_cctp <burn_tx>
"""
from __future__ import annotations

import sys
import time

import requests
from eth_account import Account
from web3 import Web3

from . import config
from .on_chain.cctp import IRIS_API, MESSAGE_TRANSMITTER_V2, DOMAIN_BASE

HYPEREVM_RPC = "https://rpc.hyperliquid.xyz/evm"
HYPEREVM_CHAIN_ID = 999

MESSAGE_TRANSMITTER_ABI = [
    {"inputs": [
        {"name": "message", "type": "bytes"},
        {"name": "attestation", "type": "bytes"},
     ],
     "name": "receiveMessage", "outputs": [{"type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
]


def main(burn_tx: str, src_domain: int = DOMAIN_BASE):
    if not burn_tx.startswith("0x"):
        burn_tx = "0x" + burn_tx
    print(f"Relaying CCTP burn {burn_tx} from domain {src_domain} → HyperEVM")

    # 1. Pull message + attestation from Iris
    url = f"{IRIS_API}/{src_domain}?transactionHash={burn_tx}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    msgs = data.get("messages", [])
    if not msgs:
        print(f"  no messages found")
        return False
    m = msgs[0]
    if m.get("status") != "complete":
        print(f"  status not complete: {m.get('status')}")
        return False
    message = m["message"]
    attestation = m["attestation"]
    print(f"  message len: {len(message)}, attestation len: {len(attestation)}")

    # 2. Build receiveMessage tx on HyperEVM
    w3 = Web3(Web3.HTTPProvider(HYPEREVM_RPC))
    account = Account.from_key(config.HL.private_key)
    addr = account.address
    mt = w3.eth.contract(address=MESSAGE_TRANSMITTER_V2, abi=MESSAGE_TRANSMITTER_ABI)

    nonce = w3.eth.get_transaction_count(addr)
    tx = mt.functions.receiveMessage(
        Web3.to_bytes(hexstr=message),
        Web3.to_bytes(hexstr=attestation),
    ).build_transaction({
        "from":    addr,
        "nonce":   nonce,
        "chainId": HYPEREVM_CHAIN_ID,
        "gas":     500_000,
        "maxFeePerGas":         w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": int(0.001e9),
    })
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    h = w3.eth.send_raw_transaction(raw)
    print(f"  submitted: {h.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    print(f"  confirmed: status={receipt.status}, gas={receipt.gasUsed}")
    return receipt.status == 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m src.recovery_relay_cctp <burn_tx>")
        sys.exit(1)
    ok = main(sys.argv[1])
    sys.exit(0 if ok else 1)
