from sqlmodel import Session, select

from desk.config import parse_series
from desk.db.models import ParentOrder, init_db, make_engine
from desk.engine.desk import ExecutionDesk
from desk.series import maybe_start_batch, parse_source
from tests.fakes import Clock, FakeOneClick

TOKENS = (("TST", 5000.0), ("TST", 2000.0))


def make(tmp_path):
    clock = Clock()
    engine = make_engine(f"sqlite:///{tmp_path}/s.db")
    init_db(engine)
    return clock, engine, ExecutionDesk(engine, FakeOneClick(clock), clock=clock)


def test_parse_series():
    assert parse_series("RHEA:25000, AURORA:10000") == (("RHEA", 25000.0), ("AURORA", 10000.0))
    assert parse_series("") == ()
    assert parse_source("series:12:RHEA:0.5") == (12, "RHEA", 0.5)
    assert parse_source("sputnik:testnet:x#1") is None


def test_batch_runs_every_horizon_and_waits_for_the_batch_to_finish(tmp_path):
    clock, engine, desk = make(tmp_path)
    first = maybe_start_batch(desk, TOKENS, (0.5, 1, 2))
    assert [o.source for o in first] == ["series:1:TST:0.5", "series:1:TST:1", "series:1:TST:2"]
    assert all(o.mode == "shadow" and o.pred_exec_bps is not None and o.pred_risk_bps > 0 for o in first)
    assert first[0].pred_risk_bps < first[2].pred_risk_bps          # longer horizon, more timing risk
    assert maybe_start_batch(desk, TOKENS, (0.5, 1, 2)) == []         # batch 1 still running

    with Session(engine) as s:
        for o in s.exec(select(ParentOrder)):
            o.status = "DONE"
            s.add(o)
        s.commit()
    second = maybe_start_batch(desk, TOKENS, (0.5, 1, 2))
    assert {parse_source(o.source)[0] for o in second} == {2}
    usd = [int(o.total_in) / 1e18 * o.arrival_mid for o in second]
    assert all(abs(u - 2000) / 2000 < 0.01 for u in usd)               # rotated to the second entry


def _done_order(engine, asset, pred_risk, timing_bps, risk_scale=None):
    from datetime import datetime, timezone
    from desk.db.models import Slice
    with Session(engine) as s:
        o = ParentOrder(origin_asset=asset, origin_decimals=0, destination_asset="usdc", destination_decimals=0,
                        total_in="100", recipient="dao", deadline_at=datetime.now(timezone.utc), slice_usd=1,
                        hard_limit_bps=250, soft_limit_bps=150, arrival_mid=1.0, status="DONE",
                        pred_risk_bps=pred_risk, risk_scale=risk_scale)
        s.add(o)
        s.commit()
        s.refresh(o)
        mid = 1 - timing_bps / 1e4
        s.add(Slice(order_id=o.id, seq=1, amount_in="100", status="SHADOW_FILLED",
                    mid_at_quote=mid, executed_out=str(int(100 * mid))))
        s.commit()


def test_risk_forecast_is_rescaled_by_its_own_track_record(tmp_path):
    clock, engine, desk = make(tmp_path)
    asset = "nep141:token.test.near"
    for i in range(7):
        _done_order(engine, asset, pred_risk=50, timing_bps=100 if i % 2 else -100)
    assert desk.risk_calibration(asset) == (1.0, 7)          # too few orders: no correction
    _done_order(engine, asset, pred_risk=100, timing_bps=-100, risk_scale=2.0)   # raw forecast was 50
    factor, n = desk.risk_calibration(asset)
    assert n == 8 and abs(factor - 2.0) < 0.05               # realised timing ~2x the raw forecast
    sigma, note, scale = desk.calibrated_sigma(asset)
    assert scale == factor and "scaled x2.00 from 8 finished orders" in note
    o = desk.create_order(origin_asset=asset, amount_in=10**21, recipient="dao", hours=1,
                          soft_bps=150, hard_bps=250)
    assert o.risk_scale == factor
