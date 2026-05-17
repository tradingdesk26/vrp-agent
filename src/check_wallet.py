"""
Verify agent wallet setup. Run after creating .env to confirm:
  - private key loads correctly
  - derived address matches expected
  - ETH balance on Base (for gas)
  - USDC balance on Base (for initial capital)
  - USDC balance on Hyperliquid (after CCTP)

Usage:
    python3 -m src.check_wallet
"""
from __future__ import annotations

import sys

import os

from . import config

# Optional sanity-check: if EXPECTED_ADDRESS env var is set, verify the
# derived address matches it. Useful when you have a known deployment
# wallet you don't want to accidentally swap keys for. Leave empty to skip.
EXPECTED_ADDRESS = os.getenv("EXPECTED_ADDRESS", "").strip()
BASE_RPC = "https://mainnet.base.org"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EURC_BASE = "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"


def check_address():
    pk = config.HL.private_key
    if not pk:
        print("FAIL: HL_PRIVATE_KEY not set in .env")
        return None
    from eth_account import Account
    try:
        addr = Account.from_key(pk).address
    except Exception as e:
        print(f"FAIL: main private key invalid: {e}")
        return None
    print(f"  MAIN wallet")
    print(f"    derived address: {addr}")
    if EXPECTED_ADDRESS:
        match = addr.lower() == EXPECTED_ADDRESS.lower()
        print(f"    expected:        {EXPECTED_ADDRESS}")
        print(f"    match: {'YES' if match else 'NO'}")
        if not match:
            print("  WARNING: address does not match EXPECTED_ADDRESS env var")
    return addr


def check_api_wallet():
    pk = config.HL.api_wallet_key
    if not pk:
        print("  API wallet: not configured (HL_API_WALLET_KEY empty)")
        print("              → main wallet will sign trades directly")
        return None
    from eth_account import Account
    try:
        addr = Account.from_key(pk).address
    except Exception as e:
        print(f"  FAIL: API wallet key invalid: {e}")
        return None
    print(f"  API wallet")
    print(f"    derived address: {addr}")
    print(f"    (must be authorized via HL UI 'Approved Agents' for main wallet)")
    return addr


def check_base_balances(addr):
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(BASE_RPC))
    if not w3.is_connected():
        print("  FAIL: cannot connect to Base RPC")
        return
    eth_bal = w3.eth.get_balance(addr) / 1e18
    print(f"  ETH balance:  {eth_bal:.6f} ETH (gas)")
    if eth_bal < 0.001:
        print("  WARNING: ETH < 0.001 — top up for gas before live ops")

    erc20 = '[{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]'
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_BASE), abi=erc20)
    eurc = w3.eth.contract(address=Web3.to_checksum_address(EURC_BASE), abi=erc20)
    usdc_bal = usdc.functions.balanceOf(addr).call() / 1e6
    eurc_bal = eurc.functions.balanceOf(addr).call() / 1e6
    print(f"  USDC balance: {usdc_bal:.4f} USDC (initial capital)")
    print(f"  EURC balance: {eurc_bal:.4f} EURC")


def check_hl(addr, api_wallet_addr=None):
    try:
        from hyperliquid.info import Info
    except ImportError:
        print("  hyperliquid-python-sdk not installed (pip install hyperliquid-python-sdk)")
        return
    info = Info(config.HL.api_url, skip_ws=True)
    import requests
    try:
        # Perp view (legacy or perp-isolated)
        state = info.user_state(addr)
        perp_margin = float(state.get("withdrawable", "0"))
        positions = state.get("assetPositions", [])
        open_pos = [p for p in positions if abs(float(p["position"]["szi"])) > 1e-9]

        # Spot view
        r = requests.post(f"{config.HL.api_url}/info",
                          json={"type": "spotClearinghouseState", "user": addr},
                          timeout=10)
        spot_balances = r.json().get("balances", []) if r.status_code == 200 else []
        spot_usdc = next((float(b["total"]) for b in spot_balances
                          if b.get("coin") == "USDC"), 0.0)

        # Unified account check
        r = requests.post(f"{config.HL.api_url}/info",
                          json={"type": "perpsAtOpenInterestCap"}, timeout=5)

        unified_total = perp_margin + spot_usdc
        print(f"  Perp margin:    ${perp_margin:.4f}")
        print(f"  Spot USDC:      ${spot_usdc:.4f}")
        print(f"  ─────────────────────")
        print(f"  Unified margin: ${unified_total:.4f}  (usable for perp trades)")
        print(f"  Open positions: {len(open_pos)}")
        for p in open_pos:
            pp = p["position"]
            print(f"    {pp['coin']}: size={pp['szi']}, entry=${pp['entryPx']}")
    except Exception as e:
        print(f"  FAIL reading HL state: {e}")
        return

    # Check authorized agents (API wallets)
    if api_wallet_addr is None:
        return
    try:
        import requests
        r = requests.post(
            f"{config.HL.api_url}/info",
            json={"type": "extraAgents", "user": addr},
            timeout=10,
        )
        if r.status_code == 200:
            agents = r.json()
            agent_addrs = [a.get("address", "").lower() for a in (agents or [])]
            print(f"  authorized API wallets: {len(agent_addrs)}")
            for a in agents or []:
                name = a.get("name", "")
                addr_str = a.get("address", "")
                marker = "  ← OUR API" if addr_str.lower() == api_wallet_addr.lower() else ""
                print(f"    {addr_str} ({name}){marker}")
            authorized = api_wallet_addr.lower() in agent_addrs
            if authorized:
                print(f"  API wallet AUTHORIZED on HL ✓")
            else:
                print(f"  WARNING: API wallet {api_wallet_addr} NOT in authorized list")
                print(f"    → go to app.hyperliquid.xyz, connect main wallet,")
                print(f"      'API' section, generate/add agent")
        else:
            print(f"  agents query returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  could not check authorized agents: {e}")


def main():
    print("=" * 60)
    print("vrp-agent wallet check")
    print("=" * 60)

    print(config.banner())

    print("─── KEYS ───")
    addr = check_address()
    if not addr:
        sys.exit(1)
    api_addr = check_api_wallet()
    print()

    print("─── BASE MAINNET ───")
    check_base_balances(addr)
    print()

    print("─── HYPERLIQUID L1 ───")
    check_hl(addr, api_wallet_addr=api_addr)
    print()

    print("=" * 60)
    print("Pre-flight checklist:")
    print("  [ ] ETH balance > 0.005 (for gas on multiple tx)")
    print("  [ ] USDC balance > $10 (initial capital)")
    print("  [ ] DRY_RUN=true in .env until first manual test")
    print("=" * 60)


if __name__ == "__main__":
    main()
