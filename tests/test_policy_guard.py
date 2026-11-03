from datetime import datetime, timedelta, timezone

from desk.engine.guard import check_live_quote
from desk.engine.policy import Limits, TickState, decide, target_in

T0 = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
LIM = Limits(soft_bps=100, hard_bps=200)


def state(minutes, done=0, cost=50, last_fill_min=None, last_cost=None, in_flight=False):
    return TickState(now=T0 + timedelta(minutes=minutes), start=T0, deadline=T0 + timedelta(hours=4),
                     total_in=1000, done_in=done, slice_in=100, cost_bps=cost,
                     last_fill_at=T0 + timedelta(minutes=last_fill_min) if last_fill_min is not None else None,
                     last_fill_cost_bps=last_cost, in_flight=in_flight)


def test_target_ends_before_deadline():
    assert target_in(state(0)) == 0
    assert target_in(state(108)) == 500        # half of 0.9 * 240 min
    assert target_in(state(216)) == 1000
    assert target_in(state(239)) == 1000


def test_good_quote_trades():
    d = decide(state(1, cost=50), LIM)
    assert d.action == "EXECUTE" and d.amount_in == 100


def test_mediocre_quote_waits_when_on_schedule():
    assert decide(state(1, cost=150), LIM).action == "WAIT"


def test_mediocre_quote_trades_when_behind():
    d = decide(state(60, done=0, cost=150), LIM)       # target ~277 -> 2.8 slices behind
    assert d.action == "EXECUTE"


def test_never_beyond_hard_limit_even_when_behind():
    d = decide(state(200, done=0, cost=250), LIM)
    assert d.action == "WAIT" and "hard" in d.reason


def test_waits_for_recovery_then_trades():
    d = decide(state(10, done=100, cost=90, last_fill_min=8, last_cost=50), LIM)
    assert d.action == "WAIT" and d.recovered is False
    d = decide(state(12, done=100, cost=55, last_fill_min=8, last_cost=50), LIM)
    assert d.action == "EXECUTE" and d.recovered is True


def test_min_gap():
    d = decide(state(10, done=100, cost=40, last_fill_min=9.5, last_cost=40), LIM)
    assert d.action == "WAIT" and "min gap" in d.reason


def test_ahead_of_schedule_waits():
    assert decide(state(5, done=300, cost=10, last_fill_min=1, last_cost=10), LIM).action == "WAIT"


def test_in_flight_and_finish():
    assert decide(state(5, in_flight=True), LIM).action == "WAIT"
    assert decide(state(5, done=1000), LIM).action == "FINISH"
    assert decide(state(240), LIM).action == "FINISH"


def test_last_slice_is_the_remainder():
    d = decide(state(215, done=950, cost=10, last_fill_min=200, last_cost=10), LIM)
    assert d.action == "EXECUTE" and d.amount_in == 50


def test_guard_catches_the_oct_24_quote():
    # 1,000 RHEA: dry said ~63.65 USDC, live came back 27.91 USDC
    r = check_live_quote(live_out=27_910_000, dry_out=63_650_000, amount_in=1000 * 10**18,
                         in_decimals=18, out_decimals=6, mid=0.0640, hard_bps=250)
    assert not r.ok and "dry" in r.reason


def test_guard_passes_normal_quote():
    r = check_live_quote(live_out=63_600_000, dry_out=63_650_000, amount_in=1000 * 10**18,
                         in_decimals=18, out_decimals=6, mid=0.0640, hard_bps=250)
    assert r.ok and r.live_cost_bps < 100
