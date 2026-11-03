import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from desk.prices.benchmark import ImpactCurve, PricePoint
from desk.prices.pretrade import DEFAULT_SIGMA, analyse, cost_at, estimate_sigma


def curve_from(bps: dict[float, float]) -> ImpactCurve:
    return ImpactCurve(mid=1.0, points=tuple(PricePoint(u, 1, 1 - b / 1e4, 1) for u, b in bps.items()))


RHEA_NOV3 = curve_from({500: 33, 1000: 42, 2500: 74, 5000: 127, 10000: 231})
WNEAR_OCT23 = curve_from({500: 20, 1000: 20, 2500: 21, 5000: 26, 10000: 31, 25000: 31})


def test_cost_interpolates_in_log_size_and_knows_its_limits():
    assert cost_at(RHEA_NOV3, 300) == pytest.approx(33)                      # below the curve: smallest size
    assert cost_at(RHEA_NOV3, 1000) == pytest.approx(42)
    mid = cost_at(RHEA_NOV3, math.sqrt(1000 * 2500))           # geometric midpoint
    assert mid == pytest.approx((42 + 74) / 2)
    assert cost_at(RHEA_NOV3, 25000) is None                   # no liquidity measured that far


def test_sigma_recovers_known_volatility():
    rng = random.Random(7)
    true_sigma = 0.02                                          # 2% per sqrt(hour)
    t0 = datetime(2026, 11, 3, tzinfo=timezone.utc)
    price, samples = 0.05, []
    for i in range(24 * 12):                                   # a day of 5-minute samples
        samples.append((t0 + timedelta(minutes=5 * i), price))
        price *= math.exp(rng.gauss(0, true_sigma * math.sqrt(5 / 60)))
    sigma, note = estimate_sigma(samples)
    assert sigma == pytest.approx(true_sigma, rel=0.15)
    assert "five-minute" in note


def test_sigma_ignores_one_bad_quote_and_falls_back_without_history():
    rng = random.Random(3)
    t0 = datetime(2026, 11, 3, tzinfo=timezone.utc)
    price, series = 0.05, []
    for i in range(72):                                        # 6 hours, 1% per sqrt(hour)
        series.append((t0 + timedelta(minutes=5 * i), price))
        price *= math.exp(rng.gauss(0, 0.01 * math.sqrt(5 / 60)))
    clean, _ = estimate_sigma(series)
    series[30] = (series[30][0], series[30][1] * 1.6)          # one crazy quote (+60%)
    sigma, _ = estimate_sigma(series)
    assert sigma < 1.25 * clean                                # barely moves the estimate
    sigma, note = estimate_sigma(series[:5])
    assert sigma == DEFAULT_SIGMA and "default" in note


def test_thin_token_gets_a_longer_horizon_than_a_liquid_one():
    rhea = analyse(25_000, RHEA_NOV3, 0.015, "", None, soft_bps=150)
    wnear = analyse(25_000, WNEAR_OCT23, 0.015, "", 31, soft_bps=150)
    # fast horizons need big RHEA slices, which are expensive or have no quote at all
    by_h = {r.hours: r for r in rhea.rows}
    assert by_h[0.5].execution_bps > by_h[4].execution_bps
    assert by_h[24].risk_bps > by_h[0.5].risk_bps
    assert rhea.recommended_hours > wnear.recommended_hours
    assert wnear.recommended_hours == 0                         # liquid: one swap now beats the agent
    assert rhea.oneshot_bps is None


def test_risk_formula():
    p = analyse(10_000, RHEA_NOV3, 0.02, "", None, soft_bps=150, horizons=(3,))
    assert len(p.rows) == 1                                     # no one-swap row without liquidity
    assert p.rows[0].risk_bps == pytest.approx(0.02 * math.sqrt(3 * 0.9 / 3) * 1e4)


def test_too_fast_horizon_is_not_feasible():
    # soft 50 bps caps RHEA slices at $1k: $25k in 30 min would mean 25 slices about a minute apart
    p = analyse(25_000, RHEA_NOV3, 0.015, "", None, soft_bps=50, horizons=(0.5, 4))
    by_h = {r.hours: r for r in p.rows}
    assert by_h[0.5].execution_bps is None and by_h[0.5].score_bps is None
    assert by_h[4].execution_bps is not None
    assert p.recommended_hours == 4
