import pytest

from desk.oneclick.client import NoLiquidity
from desk.prices.benchmark import (ImpactCurve, PricePoint, TokenInfo, build_curve,
                                   impact_bps, micro_mid, sample)

TOKEN = TokenInfo("nep141:token.rhealab.near", 18, 0.064)
TRUE_MID = 0.065
FEE = 0.002


def amm_quote(amount_in: int) -> int:
    """Fake pool: price falls linearly with size (10 bps per $1k), fee taken on top."""
    qty = amount_in / 1e18
    usd = qty * TOKEN.ref_price_usd
    if usd > 50_000:
        raise NoLiquidity(400, "No liquidity available")
    price = TRUE_MID * (1 - 0.001 * usd / 1_000) * (1 - FEE)
    return int(qty * price * 1e6)


def test_micro_mid_recovers_true_mid():
    pts = sample(TOKEN, 6, (300, 1_000, 3_000), amm_quote)
    assert micro_mid(pts) == pytest.approx(TRUE_MID, rel=1e-4)


def test_no_liquidity_sizes_are_skipped():
    pts = sample(TOKEN, 6, (1_000, 100_000), amm_quote)
    assert [p.usd_size for p in pts] == [1_000]


def test_positive_slope_falls_back_to_best_price():
    pts = [PricePoint(300, 1, 0.060, 0.0601), PricePoint(1000, 1, 0.061, 0.0611)]
    assert micro_mid(pts) == 0.0611


def test_empty_points():
    with pytest.raises(ValueError):
        micro_mid([])


def test_curve_and_slice_size():
    curve = build_curve(TOKEN, 6, amm_quote)
    bps = dict(curve.bps())
    # $5k: 50 bps price impact + 20 bps fee
    assert bps[5_000] == pytest.approx(70, abs=0.5)
    assert bps[500] < bps[5_000] < bps[25_000]
    assert curve.max_size_within(100) == 5_000
    assert curve.max_size_within(1) is None


def test_impact_sign():
    assert impact_bps(0.99, 1.0) == pytest.approx(100)
    assert impact_bps(1.01, 1.0) == pytest.approx(-100)
