"""
Uniswap v3/v4 liquidity math.

For a position bounded by ticks [tick_lower, tick_upper] at current price P,
the amounts and liquidity L are related by:

  amount0 = L * (sqrt(P_b) - sqrt(P)) / (sqrt(P) * sqrt(P_b))    if P in range
          = L * (sqrt(P_b) - sqrt(P_a)) / (sqrt(P_a) * sqrt(P_b)) if P < P_a
          = 0                                                     if P > P_b

  amount1 = L * (sqrt(P) - sqrt(P_a))     if P in range
          = 0                              if P < P_a
          = L * (sqrt(P_b) - sqrt(P_a))   if P > P_b

For full-range positions (tick_lower=-887220, tick_upper=887220 at ts=60):
  sqrt(P_a) is tiny, sqrt(P_b) is huge
  → amount0 ≈ L / sqrt(P_current)
  → amount1 ≈ L * sqrt(P_current)

We use exact Q96 math from sqrtPriceX96.
"""
from __future__ import annotations

# Q96 fixed-point divisor
Q96 = 1 << 96


def liquidity_from_amount0(
    sqrt_price_x96: int,
    sqrt_price_upper_x96: int,
    amount0: int,
) -> int:
    """Given amount0 and price range, return max L (P in range or below)."""
    if sqrt_price_x96 >= sqrt_price_upper_x96:
        return 0
    # L = amount0 * sqrt(P) * sqrt(P_b) / (sqrt(P_b) - sqrt(P))
    intermediate = (sqrt_price_x96 * sqrt_price_upper_x96) // Q96
    denominator = sqrt_price_upper_x96 - sqrt_price_x96
    return amount0 * intermediate // denominator


def liquidity_from_amount1(
    sqrt_price_lower_x96: int,
    sqrt_price_x96: int,
    amount1: int,
) -> int:
    """Given amount1 and price range, return max L (P in range or above)."""
    if sqrt_price_x96 <= sqrt_price_lower_x96:
        return 0
    # L = amount1 * Q96 / (sqrt(P) - sqrt(P_a))
    return amount1 * Q96 // (sqrt_price_x96 - sqrt_price_lower_x96)


def liquidity_from_amounts(
    sqrt_price_x96: int,
    sqrt_price_lower_x96: int,
    sqrt_price_upper_x96: int,
    amount0: int,
    amount1: int,
) -> int:
    """Max L achievable with both amounts. Returns binding L."""
    if sqrt_price_x96 <= sqrt_price_lower_x96:
        return liquidity_from_amount0(
            sqrt_price_lower_x96, sqrt_price_upper_x96, amount0
        )
    elif sqrt_price_x96 < sqrt_price_upper_x96:
        l0 = liquidity_from_amount0(sqrt_price_x96, sqrt_price_upper_x96, amount0)
        l1 = liquidity_from_amount1(sqrt_price_lower_x96, sqrt_price_x96, amount1)
        return min(l0, l1)
    else:
        return liquidity_from_amount1(
            sqrt_price_lower_x96, sqrt_price_upper_x96, amount1
        )


def amount0_from_liquidity(
    sqrt_price_x96: int,
    sqrt_price_upper_x96: int,
    liquidity: int,
) -> int:
    """Inverse: amount0 required for given L."""
    if sqrt_price_x96 >= sqrt_price_upper_x96:
        return 0
    intermediate = (sqrt_price_x96 * sqrt_price_upper_x96) // Q96
    return liquidity * (sqrt_price_upper_x96 - sqrt_price_x96) // intermediate


def amount1_from_liquidity(
    sqrt_price_lower_x96: int,
    sqrt_price_x96: int,
    liquidity: int,
) -> int:
    """Inverse: amount1 required for given L."""
    if sqrt_price_x96 <= sqrt_price_lower_x96:
        return 0
    return liquidity * (sqrt_price_x96 - sqrt_price_lower_x96) // Q96


def tick_to_sqrt_price_x96(tick: int) -> int:
    """Solidity TickMath.getSqrtPriceAtTick port. Returns sqrt(1.0001^tick) * 2^96."""
    abs_tick = -tick if tick < 0 else tick
    assert abs_tick <= 887272, "tick out of range"

    ratio = 0xfffcb933bd6fad37aa2d162d1a594001 if (abs_tick & 0x1) else 0x100000000000000000000000000000000
    if abs_tick & 0x2:     ratio = (ratio * 0xfff97272373d413259a46990580e213a) >> 128
    if abs_tick & 0x4:     ratio = (ratio * 0xfff2e50f5f656932ef12357cf3c7fdcc) >> 128
    if abs_tick & 0x8:     ratio = (ratio * 0xffe5caca7e10e4e61c3624eaa0941cd0) >> 128
    if abs_tick & 0x10:    ratio = (ratio * 0xffcb9843d60f6159c9db58835c926644) >> 128
    if abs_tick & 0x20:    ratio = (ratio * 0xff973b41fa98c081472e6896dfb254c0) >> 128
    if abs_tick & 0x40:    ratio = (ratio * 0xff2ea16466c96a3843ec78b326b52861) >> 128
    if abs_tick & 0x80:    ratio = (ratio * 0xfe5dee046a99a2a811c461f1969c3053) >> 128
    if abs_tick & 0x100:   ratio = (ratio * 0xfcbe86c7900a88aedcffc83b479aa3a4) >> 128
    if abs_tick & 0x200:   ratio = (ratio * 0xf987a7253ac413176f2b074cf7815e54) >> 128
    if abs_tick & 0x400:   ratio = (ratio * 0xf3392b0822b70005940c7a398e4b70f3) >> 128
    if abs_tick & 0x800:   ratio = (ratio * 0xe7159475a2c29b7443b29c7fa6e889d9) >> 128
    if abs_tick & 0x1000:  ratio = (ratio * 0xd097f3bdfd2022b8845ad8f792aa5825) >> 128
    if abs_tick & 0x2000:  ratio = (ratio * 0xa9f746462d870fdf8a65dc1f90e061e5) >> 128
    if abs_tick & 0x4000:  ratio = (ratio * 0x70d869a156d2a1b890bb3df62baf32f7) >> 128
    if abs_tick & 0x8000:  ratio = (ratio * 0x31be135f97d08fd981231505542fcfa6) >> 128
    if abs_tick & 0x10000: ratio = (ratio * 0x9aa508b5b7a84e1c677de54f3e99bc9) >> 128
    if abs_tick & 0x20000: ratio = (ratio * 0x5d6af8dedb81196699c329225ee604) >> 128
    if abs_tick & 0x40000: ratio = (ratio * 0x2216e584f5fa1ea926041bedfe98) >> 128
    if abs_tick & 0x80000: ratio = (ratio * 0x48a170391f7dc42444e8fa2) >> 128

    if tick > 0:
        ratio = ((1 << 256) - 1) // ratio
    # downcast to uint160 boundary
    return (ratio >> 32) + (1 if ratio & ((1 << 32) - 1) else 0)


if __name__ == "__main__":
    # Sanity: replicate user's existing LP
    # tokenId 2317884: L=5408294, ticks [-887220, 887220], EURC≈5e6, USDC≈5.85e6
    sqrt_p_x96 = 85697863746168646039218808897  # from pool init
    sqrt_pl = tick_to_sqrt_price_x96(-887220)
    sqrt_pu = tick_to_sqrt_price_x96(887220)

    # Compute L from amounts
    L = liquidity_from_amounts(sqrt_p_x96, sqrt_pl, sqrt_pu, 5_000_000, 5_850_000)
    print(f"L from amounts (5e6 EURC, 5.85e6 USDC): {L:,}")
    print(f"  expected: ~5,408,294")

    # Inverse: amounts for L=5408294
    amt0 = amount0_from_liquidity(sqrt_p_x96, sqrt_pu, 5_408_294)
    amt1 = amount1_from_liquidity(sqrt_pl, sqrt_p_x96, 5_408_294)
    print(f"\namounts for L=5408294: amt0={amt0:,} EURC raw, amt1={amt1:,} USDC raw")
