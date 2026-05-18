"""
One-shot: deposit all HyperEVM USDC to HL PERPS via CoreDepositWallet.

Used when recovery_relay_cctp.py delivered USDC to HyperEVM but agent
got stuck in UNKNOWN state because state classifier doesn't look at
HyperEVM USDC balance.

Usage:
  python3 -m src.recovery_deposit_to_hl
"""
from __future__ import annotations
import time
from eth_account import Account
from web3 import Web3

from . import config
from .phase2_forward import (
    USDC_HYPEREVM, CORE_DEPOSIT_WALLET, CORE_DEPOSIT_ABI,
    HYPEREVM_CHAIN_ID, DEST_PERPS, ERC20_ABI, hyperevm_w3,
)


def main():
    w3 = hyperevm_w3()
    account = Account.from_key(config.HL.private_key)
    addr = account.address

    usdc = w3.eth.contract(address=USDC_HYPEREVM, abi=ERC20_ABI)
    cdw  = w3.eth.contract(address=CORE_DEPOSIT_WALLET, abi=CORE_DEPOSIT_ABI)

    bal = usdc.functions.balanceOf(addr).call()
    print(f"HyperEVM USDC balance: ${bal/1e6:.4f}")
    if bal < 1_000_000:
        print("nothing to deposit (< $1)")
        return

    nonce = w3.eth.get_transaction_count(addr)

    def send(fn, gas_limit=200_000):
        nonlocal nonce
        tx = fn.build_transaction({
            "from":    addr,
            "nonce":   nonce,
            "chainId": HYPEREVM_CHAIN_ID,
            "gas":     gas_limit,
            "maxFeePerGas":         w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": int(0.001e9),
        })
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        nonce += 1
        print(f"  submitted: {h.hex()}")
        r = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        print(f"  confirmed: status={r.status}, gas={r.gasUsed}")
        return r

    # Approve
    cur_allow = usdc.functions.allowance(addr, CORE_DEPOSIT_WALLET).call()
    if cur_allow < bal:
        print("Approving USDC → CoreDepositWallet")
        send(usdc.functions.approve(CORE_DEPOSIT_WALLET, 2**256 - 1), gas_limit=100_000)
        time.sleep(2)
    else:
        print(f"Allowance sufficient: {cur_allow/1e6:.2f} USDC")

    # Deposit
    print(f"\nDepositing ${bal/1e6:.4f} → HL PERPS (dest={DEST_PERPS})")
    send(cdw.functions.deposit(bal, DEST_PERPS), gas_limit=200_000)

    print(f"\nDone. Wait ~30-60s for HL credit, then service should recover.")


if __name__ == "__main__":
    main()
