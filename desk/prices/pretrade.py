"""Pre-trade analysis: how long should the DAO take to sell?

Almgren-Chriss trade-off with the agent's own cost model:
  * selling faster means bigger slices, and each slice costs more (measured impact curve);
  * selling slower means smaller slices, but the price can drift away while we wait.

For a horizon T the agent picks a slice size (same rule as live orders), reads its
expected execution cost from the impact curve, and estimates the timing risk of a
linear schedule as one standard deviation of implementation shortfall:

  risk(T) = sigma * sqrt(T_eff / 3),  T_eff = 0.9 T   (Almgren & Chriss, 2000)

where sigma is the token's volatility per sqrt(hour). The recommended horizon minimises
expected cost + risk_aversion * risk. With risk_aversion = 1 that reads as
"cost if the market moves one sigma against us". Horizon 0 is a single swap now; for a
liquid token it is often the honest answer, and the report says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from desk.engine.policy import SCHEDULE_END_FRACTION
from desk.prices.benchmark import ImpactCurve

TARGET_SLICE_SPACING_S = 300          # aim for roughly one slice every 5 minutes
MIN_SLICE_SPACING_S = 120             # faster than this, liquidity has no time to come back
HORIZONS_H = (0.5, 1, 2, 4, 8, 12, 24)
DEFAULT_SIGMA = 0.015                 # 1.5% per sqrt(hour) when there is too little history
MIN_VOL_SAMPLES = 12
VOL_BUCKET_S = 300


def choose_slice_usd(order_usd: float, hours: float, curve: ImpactCurve, soft_bps: float) -> float:
    """Smallest slice that still fits the horizon, never above what the soft limit allows.

    Smaller slices cost less (RHEA, Nov 3: $1k ~42 bps, $5k ~127 bps), as long as liquidity
    recovers between them; the horizon caps how many slices there is time for.
    """
    max_slices = max(1, int(hours * 3600 * SCHEDULE_END_FRACTION / TARGET_SLICE_SPACING_S))
    smallest = min(p.usd_size for p in curve.points)
    size = max(order_usd / max_slices, smallest)
    cap = curve.max_size_within(soft_bps)
    return min(size, cap) if cap else smallest


def cost_at(curve: ImpactCurve, usd: float) -> float | None:
    """Expected cost (bps vs benchmark) of one swap of `usd`, linear in log(size) between
    measured points. Below the smallest size: the smallest size's cost. Above the largest
    size that had liquidity: unknown (None)."""
    pts = sorted(curve.bps())
    if not pts:
        return None
    if usd <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= usd <= x1:
            w = (math.log(usd) - math.log(x0)) / (math.log(x1) - math.log(x0))
            return y0 + w * (y1 - y0)
    return None


def estimate_sigma(samples: Sequence[tuple[datetime, float]]) -> tuple[float, str]:
    """Volatility per sqrt(hour) from benchmark samples, robust to single bad quotes.

    Samples are put in 5-minute buckets (last value), then each log return is scaled by
    sqrt(gap in hours) and the spread is measured with the median absolute deviation.
    """
    buckets: dict[int, float] = {}
    for ts, mid in sorted(samples):
        if mid and mid > 0:
            buckets[int(ts.timestamp()) // VOL_BUCKET_S] = mid
    keys = sorted(buckets)
    norm = []
    for a, b in zip(keys, keys[1:]):
        gap_h = (b - a) * VOL_BUCKET_S / 3600
        if gap_h > 2:          # skip gaps (worker was down): one return over hours is not comparable
            continue
        norm.append(math.log(buckets[b] / buckets[a]) / math.sqrt(gap_h))
    if len(norm) < MIN_VOL_SAMPLES:
        return DEFAULT_SIGMA, f"default {DEFAULT_SIGMA:.1%}/√h (only {len(norm)} returns of history)"
    med = sorted(norm)[len(norm) // 2]
    mad = sorted(abs(x - med) for x in norm)[len(norm) // 2]
    sigma = 1.4826 * mad
    if sigma <= 0:
        sigma = math.sqrt(sum(x * x for x in norm) / len(norm))
    return sigma, f"{sigma:.2%}/√h from {len(norm)} five-minute returns"


@dataclass(frozen=True)
class HorizonRow:
    hours: float
    slice_usd: float
    slices: int
    execution_bps: float | None
    risk_bps: float
    score_bps: float | None


@dataclass(frozen=True)
class Pretrade:
    order_usd: float
    sigma: float
    sigma_note: str
    oneshot_bps: float | None        # None = no liquidity for the whole size
    rows: tuple[HorizonRow, ...]
    recommended_hours: float | None
    risk_aversion: float


def analyse(order_usd: float, curve: ImpactCurve, sigma: float, sigma_note: str,
            oneshot_bps: float | None, soft_bps: float, risk_aversion: float = 1.0,
            horizons: Sequence[float] = HORIZONS_H) -> Pretrade:
    rows = []
    if oneshot_bps is not None:
        # hours = 0: one swap right now, no timing risk. For liquid tokens this often wins.
        rows.append(HorizonRow(0.0, order_usd, 1, oneshot_bps, 0.0, oneshot_bps))
    for h in horizons:
        slice_usd = choose_slice_usd(order_usd, h, curve, soft_bps)
        n = max(1, math.ceil(order_usd / slice_usd))
        execution = cost_at(curve, slice_usd)
        if n * MIN_SLICE_SPACING_S > h * 3600 * SCHEDULE_END_FRACTION:
            execution = None   # slices capped by the soft limit would come faster than liquidity returns
        risk = sigma * math.sqrt(h * SCHEDULE_END_FRACTION / 3) * 1e4
        score = None if execution is None else execution + risk_aversion * risk
        rows.append(HorizonRow(h, slice_usd, n, execution, risk, score))
    feasible = [r for r in rows if r.score_bps is not None]
    best = min(feasible, key=lambda r: r.score_bps).hours if feasible else None
    return Pretrade(order_usd, sigma, sigma_note, oneshot_bps, tuple(rows), best, risk_aversion)
