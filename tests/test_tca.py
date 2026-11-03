from datetime import datetime, timezone

import pytest

from desk.db.models import ParentOrder, Slice
from desk.tca.metrics import build_report

T = datetime(2026, 11, 5, tzinfo=timezone.utc)


def order(**kw):
    base = dict(id=1, origin_asset="x", origin_decimals=18, destination_asset="u", destination_decimals=6,
                total_in=str(100 * 10**18), recipient="dao", deadline_at=T, slice_usd=1, hard_limit_bps=1,
                soft_limit_bps=1, arrival_mid=2.0, oneshot_out=str(int(190 * 1e6)))
    base.update(kw)
    return ParentOrder(**base)


def sl(qty, out, mid, status="SHADOW_FILLED", worst=None, rec=None):
    return Slice(order_id=1, seq=1, amount_in=str(qty * 10**18), executed_out=str(int(out * 1e6)),
                 mid_at_quote=mid, status=status, shadow_worst_out=str(int(worst * 1e6)) if worst else None,
                 recovery_s=rec)


def test_decomposition_matches_plan_example():
    # arrival 2.000, mids average 1.995, executed average 1.988 -> IS 60 = 25 timing + 35 execution
    r = build_report(order(), [sl(50, 50 * 1.990, 1.997), sl(50, 50 * 1.986, 1.993)])
    assert r.avg_price == pytest.approx(1.988)
    assert r.is_bps == pytest.approx(60)
    assert r.timing_bps == pytest.approx(25)
    assert r.execution_bps == pytest.approx(35)
    assert r.timing_bps + r.execution_bps == pytest.approx(r.is_bps)
    assert r.oneshot_bps == pytest.approx(500)      # 190 for 100 tokens at 2.0
    assert r.savings_bps == pytest.approx(440)
    assert r.savings_usd == pytest.approx(440 / 1e4 * 2.0 * 100)
    assert r.progress == 1.0


def test_shadow_worst_case_and_no_liquidity_oneshot():
    r = build_report(order(oneshot_out=None), [sl(100, 196, 2.0, worst=190)])
    assert r.oneshot_bps is None and r.savings_bps is None
    assert r.worst_case_is_bps == pytest.approx(500)


def test_empty_and_ignores_unfilled():
    r = build_report(order(), [sl(50, 99, 2.0, status="SKIPPED")])
    assert r.slices == 0 and r.is_bps is None and r.oneshot_bps == pytest.approx(500)
