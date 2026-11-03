from datetime import timedelta

from sqlmodel import Session

from desk.config import RHEA, USDC_NEAR
from desk.db.models import (ParentOrder, QuoteSnapshot, Slice, active_orders, init_db,
                            make_engine, unsettled_slices, utcnow)


def test_roundtrip_and_queries(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    big = 370_000 * 10**18     # > BIGINT, must survive as a string
    with Session(engine) as s:
        o = ParentOrder(origin_asset=RHEA, origin_decimals=18, destination_asset=USDC_NEAR,
                        destination_decimals=6, total_in=str(big), recipient="dao.sputnik-dao.near",
                        deadline_at=utcnow() + timedelta(hours=4), slice_usd=1000,
                        hard_limit_bps=250, soft_limit_bps=150)
        s.add(o)
        s.commit()
        s.refresh(o)
        s.add_all([
            Slice(order_id=o.id, seq=1, amount_in=str(10**21), status="SUCCESS"),
            Slice(order_id=o.id, seq=2, amount_in=str(10**21), status="SUBMITTED"),
            Slice(order_id=o.id, seq=3, amount_in=str(10**21), status="SIGNED"),
            Slice(order_id=o.id, seq=4, amount_in=str(10**21)),
            QuoteSnapshot(order_id=o.id, kind="micro", asset=RHEA, usd_size=300, amount_in="1"),
        ])
        s.commit()

    with Session(engine) as s:
        [o] = active_orders(s)
        assert o.total_in_int() == big
        assert sorted(x.seq for x in unsettled_slices(s, o.id)) == [2, 3]


def test_postgres_url_rewrite():
    from desk.db import models
    seen = {}
    real = models.create_engine
    models.create_engine = lambda url, **kw: seen.setdefault("url", url)
    try:
        make_engine("postgres://u:p@host/db")
        assert seen["url"] == "postgresql+psycopg://u:p@host/db"
    finally:
        models.create_engine = real
