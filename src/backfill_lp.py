"""
One-shot backfill: register existing on-chain LP position into SQLite.

For tokenId 2334439 (agent's first LP minted in ETH/USDC pool during
bootstrap_pool2.py before tokenId persistence was wired).
"""
from datetime import datetime

from .on_chain.client import BaseClient, HOOK_ETH_USDC
from .on_chain.lp_manager import LPManager
from .pnl_tracker import PnLTracker

KNOWN_LP_TOKEN_ID = 2334439
KNOWN_MINT_TX     = "0x02648d217010054a460915d9c28eab68a232b6684f0292e3e262ef450e8c97fd"


def main():
    base = BaseClient()
    lp = LPManager(base)
    tracker = PnLTracker()

    pos = lp.read_position(KNOWN_LP_TOKEN_ID)
    print(f"Verifying tokenId {KNOWN_LP_TOKEN_ID}:")
    print(f"  exists:     {pos.exists}")
    print(f"  owner:      {pos.owner}")
    print(f"  liquidity:  {pos.liquidity}")
    print(f"  ticks:      [{pos.tick_lower}, {pos.tick_upper}]")
    print(f"  pool hook:  {pos.pool_key['hooks'] if pos.pool_key else 'N/A'}")

    if not pos.exists:
        print("  FAIL: position does not exist")
        return
    if pos.owner.lower() != base.address.lower():
        print(f"  FAIL: owner mismatch (expected {base.address})")
        return

    # Get mint tx receipt for block + amounts (approximations)
    receipt = base.w3.eth.get_transaction_receipt(KNOWN_MINT_TX)
    print(f"  mint block: {receipt.blockNumber}")

    # Approximations of initial amounts (from L computation at mint time):
    # L=47836680873, sqrtP at mint, full range
    # a0 = ~0.000999579 ETH, a1 = ~$2.288
    initial_amount0 = 999999579166031
    initial_amount1 = 2288348

    tracker.record_lp_mint(
        token_id=KNOWN_LP_TOKEN_ID,
        pool_label="ETH/USDC",
        hook=HOOK_ETH_USDC,
        tick_lower=pos.tick_lower,
        tick_upper=pos.tick_upper,
        ts=datetime.utcnow().isoformat(),
        block=receipt.blockNumber,
        tx=KNOWN_MINT_TX,
        initial_amount0=initial_amount0,
        initial_amount1=initial_amount1,
    )
    print(f"\n  saved to lp_positions table")

    active = tracker.active_lp()
    print(f"\nActive LP (from SQLite):")
    for k, v in active.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
