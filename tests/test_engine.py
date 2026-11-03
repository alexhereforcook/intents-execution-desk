from sqlmodel import Session

from desk.db.models import ParentOrder, init_db, make_engine, order_slices
from desk.engine.desk import ExecutionDesk
from desk.near.signer import AgentSigner
from tests.fakes import TOKEN, Clock, FakeOneClick

DAO = "treasury.sputnik-dao.near"
SELL = 300_000 * 10**18          # ~$19.5k at 0.065


def setup(tmp_path, mode="shadow", **pool):
    clock = Clock()
    fake = FakeOneClick(clock, **pool)
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    signer = AgentSigner(bytes(range(32))) if mode == "live" else None
    desk = ExecutionDesk(engine, fake, signer=signer, clock=clock)
    o = desk.create_order(origin_asset=TOKEN, amount_in=SELL, recipient=DAO, hours=4,
                          soft_bps=150, hard_bps=400, mode=mode)
    return clock, fake, engine, desk, o


def run(clock, desk, order_id, every_s=30, max_ticks=2000):
    for _ in range(max_ticks):
        d = desk.tick(order_id)
        if d.action == "FINISH":
            return
        clock.advance(every_s)
    raise AssertionError("order did not finish")


def test_create_order_sets_benchmark_and_slice(tmp_path):
    _, fake, _, _, o = setup(tmp_path)
    assert abs(o.arrival_mid - 0.065) / 0.065 < 1e-3
    assert 0 < int(o.slice_in) < SELL
    assert int(o.oneshot_out) > 0


def test_shadow_run_sells_everything_and_brackets_outcome(tmp_path):
    clock, fake, engine, desk, o = setup(tmp_path)
    run(clock, desk, o.id)
    with Session(engine) as s:
        order = s.get(ParentOrder, o.id)
        slices = order_slices(s, o.id)
    assert order.status == "DONE"
    assert all(x.status == "SHADOW_FILLED" for x in slices)
    assert sum(int(x.amount_in) for x in slices) == SELL
    best = sum(int(x.executed_out) for x in slices)
    worst = sum(int(x.shadow_worst_out) for x in slices)
    # full recovery beats the one-shot swap; zero recovery is about the one-shot swap
    assert best > int(order.oneshot_out)
    assert worst <= best
    assert abs(worst - int(order.oneshot_out)) / int(order.oneshot_out) < 0.01
    assert fake.submitted == []                       # shadow never sends anything


def test_live_run_waits_for_recovery_and_beats_one_shot(tmp_path):
    clock, fake, engine, desk, o = setup(tmp_path, mode="live", recovery_half_life_s=120)
    run(clock, desk, o.id)
    with Session(engine) as s:
        order = s.get(ParentOrder, o.id)
        slices = order_slices(s, o.id)
    filled = [x for x in slices if x.status == "SUCCESS"]
    assert sum(int(x.amount_in) for x in filled) == SELL
    assert sum(int(x.executed_out) for x in filled) > int(order.oneshot_out)
    gaps = [(b.filled_at - a.filled_at).total_seconds() for a, b in zip(filled, filled[1:])]
    assert min(gaps) >= 60
    assert any(x.recovery_s for x in filled)          # recovery was measured


def test_guard_blocks_bad_live_quote(tmp_path):
    clock, fake, engine, desk, o = setup(tmp_path, mode="live")
    fake.live_quote_haircut = 0.56                    # the Oct 24 case
    for _ in range(5):
        desk.tick(o.id)
        clock.advance(30)
    with Session(engine) as s:
        slices = order_slices(s, o.id)
    assert slices and all(x.status == "SKIPPED" and "guard" in x.skip_reason for x in slices)
    assert fake.submitted == [] and fake.generated == 0


def test_restart_reconciles_instead_of_resending(tmp_path):
    clock, fake, engine, desk, o = setup(tmp_path, mode="live")
    desk.tick(o.id)                                   # first slice goes out
    with Session(engine) as s:
        [sl] = order_slices(s, o.id)
        sl.status = "SUBMITTED"                       # pretend we crashed before seeing SUCCESS
        s.add(sl)
        s.commit()
    sent = len(fake.submitted)
    fresh = ExecutionDesk(engine, fake, signer=AgentSigner(bytes(range(32))), clock=clock)
    clock.advance(30)
    fresh.tick(o.id)
    with Session(engine) as s:
        first = order_slices(s, o.id)[0]
    assert first.status == "SUCCESS"
    assert len(fake.submitted) == sent                # the slice was not sent twice


def test_signed_but_never_submitted_is_failed_after_quote_expiry(tmp_path):
    clock, fake, engine, desk, o = setup(tmp_path, mode="live")
    real_submit = fake.submit_intent
    fake.submit_intent = lambda signed: (_ for _ in ()).throw(ConnectionError("crash"))
    try:
        desk.tick(o.id)
    except ConnectionError:
        pass
    fake.submit_intent = real_submit
    with Session(engine) as s:
        assert order_slices(s, o.id)[0].status == "SIGNED"
    clock.advance(200)                                # past the 180 s quote deadline
    desk.tick(o.id)
    with Session(engine) as s:
        first = order_slices(s, o.id)[0]
    assert first.status == "FAILED" and "not delivered" in first.skip_reason
