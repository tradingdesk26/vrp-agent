"""
One-shot helper: transfer USDC from HL Spot account → HL Perp account.

Run:  python3 -m src.transfer_spot_to_perp [amount_usdc]
Default amount: full spot USDC balance.
"""
import sys

from . import config


def main():
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from eth_account import Account
    import requests

    main_addr = Account.from_key(config.HL.private_key).address
    signer_key = config.HL.api_wallet_key or config.HL.private_key
    wallet = Account.from_key(signer_key)

    # Read current spot USDC
    r = requests.post(
        f"{config.HL.api_url}/info",
        json={"type": "spotClearinghouseState", "user": main_addr},
        timeout=10,
    )
    balances = r.json().get("balances", [])
    spot_usdc = next((float(b["total"]) for b in balances if b.get("coin") == "USDC"), 0.0)
    print(f"  Spot USDC: ${spot_usdc:.4f}")
    if spot_usdc < 0.01:
        print("  nothing to transfer")
        return

    amount = float(sys.argv[1]) if len(sys.argv) > 1 else spot_usdc
    print(f"  transferring ${amount:.4f} spot → perp")

    if config.RISK.dry_run:
        print("  [DRY_RUN] — would call Exchange.usd_class_transfer(amount, to_perp=True)")
        print("  Set DRY_RUN=false in .env to actually transfer")
        return

    exchange = Exchange(wallet, config.HL.api_url, account_address=main_addr)
    result = exchange.usd_class_transfer(amount, to_perp=True)
    print(f"  result: {result}")


if __name__ == "__main__":
    main()
