"""Benchmark price and impact curve from 1Click dry quotes.

Why not GET /v0/tokens `price`: measured on Oct 23-24, 2026 it jumps by 40-100 bps
while the quoted depth stays flat (RHEA $5k vs $500 gap: 73 +/- 2.4 bps over 18 hours).

Micro-mid: dry quotes at a few small sizes, the 1Click fee added back, then a straight
line through (size, price) extended to size 0. Very small sizes (~$50) are avoided:
fixed costs distort them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from desk.oneclick.client import NoLiquidity

# 1Click platform fee with an API key: 20 bps, 1 bp for stablecoin pairs
# https://docs.near-intents.org/resources/fees.md
ONECLICK_FEE_BPS = 20

MICRO_SIZES_USD = (300, 1_000, 3_000)
CURVE_SIZES_USD = (500, 1_000, 2_500, 5_000, 10_000, 25_000)

# quote_fn(amount_in_smallest_units) -> amount_out_smallest_units; raises NoLiquidity
QuoteFn = Callable[[int], int]


@dataclass(frozen=True)
class TokenInfo:
    asset_id: str
    decimals: int
    ref_price_usd: float      # only used to turn a dollar size into a token amount


@dataclass(frozen=True)
class PricePoint:
    usd_size: float
    token_qty: float
    price: float              # quote units per token, as executed (fee included)
    gross_price: float        # fee added back


def _units(token: TokenInfo, usd: float) -> int:
    return int(usd / token.ref_price_usd * 10**token.decimals)


def sample(token: TokenInfo, out_decimals: int, sizes_usd: Sequence[float],
           quote_fn: QuoteFn, fee_bps: float = ONECLICK_FEE_BPS) -> list[PricePoint]:
    points = []
    for usd in sizes_usd:
        amount_in = _units(token, usd)
        try:
            out = quote_fn(amount_in)
        except NoLiquidity:
            continue
        qty = amount_in / 10**token.decimals
        price = (out / 10**out_decimals) / qty
        points.append(PricePoint(usd, qty, price, price / (1 - fee_bps / 1e4)))
    return points


def micro_mid(points: Sequence[PricePoint]) -> float:
    """Gross price extrapolated to zero size (least squares line through the points)."""
    if not points:
        raise ValueError("no quotes to build a benchmark from")
    if len(points) == 1:
        return points[0].gross_price
    xs = [p.usd_size for p in points]
    ys = [p.gross_price for p in points]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    if slope >= 0:   # price should not improve with size; treat as noise
        return max(ys)
    return my - slope * mx


def impact_bps(price: float, mid: float) -> float:
    """How much worse than mid a sell price is, in bps (positive = worse)."""
    return (mid - price) / mid * 1e4


@dataclass(frozen=True)
class ImpactCurve:
    mid: float
    points: tuple[PricePoint, ...]

    def bps(self) -> list[tuple[float, float]]:
        return [(p.usd_size, impact_bps(p.price, self.mid)) for p in self.points]

    def max_size_within(self, limit_bps: float) -> float | None:
        """Largest sampled size whose all-in cost vs mid stays within limit_bps."""
        ok = [usd for usd, b in self.bps() if b <= limit_bps]
        return max(ok) if ok else None


def build_curve(token: TokenInfo, out_decimals: int, quote_fn: QuoteFn,
                micro_sizes: Sequence[float] = MICRO_SIZES_USD,
                curve_sizes: Sequence[float] = CURVE_SIZES_USD,
                fee_bps: float = ONECLICK_FEE_BPS) -> ImpactCurve:
    micro = sample(token, out_decimals, micro_sizes, quote_fn, fee_bps)
    mid = micro_mid(micro)
    pts = sample(token, out_decimals, curve_sizes, quote_fn, fee_bps)
    return ImpactCurve(mid=mid, points=tuple(pts))
