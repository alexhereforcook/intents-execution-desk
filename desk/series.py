"""Evidence series: the same order run at several horizons at once, over and over.

Each batch sells one token at every horizon in DESK_SERIES_HOURS (default 0.5, 1, 2, 4 h)
in shadow mode, all started at the same moment, so they face the same market. When the
whole batch is finished the next token in DESK_SERIES starts. Every order stores the
pre-trade forecast (execution cost, timing risk), so the dashboard can show forecast vs
actual by token and horizon, across different times of day.
"""

from __future__ import annotations

import logging
import re

from sqlmodel import Session, select

from desk.db.models import ParentOrder
from desk.engine.desk import ExecutionDesk

log = logging.getLogger("desk.series")
PREFIX = "series:"
DEMO_TREASURY = "treasury.sputnik-dao.near"
SOFT_BPS, HARD_BPS = 150.0, 250.0        # the dashboard defaults: what a DAO would vote for
_SOURCE = re.compile(r"series:(\d+):([^:]+):([\d.]+)")


def parse_source(source: str | None) -> tuple[int, str, float] | None:
    m = _SOURCE.fullmatch(source or "")
    return (int(m[1]), m[2], float(m[3])) if m else None


def is_series(o: ParentOrder) -> bool:
    return (o.source or "").startswith(PREFIX)


def maybe_start_batch(desk: ExecutionDesk, tokens, hours, recipient: str = DEMO_TREASURY) -> list[ParentOrder]:
    """Start the next batch if no series order is running. Returns the orders created."""
    if not tokens or not hours:
        return []
    with Session(desk.engine) as s:
        series = [o for o in s.exec(select(ParentOrder).where(ParentOrder.source.startswith(PREFIX)))]
    if any(o.status == "ACTIVE" for o in series):
        return []
    last = max((p[0] for p in map(parse_source, (o.source for o in series)) if p), default=0)
    batch = last + 1
    sym, usd = tokens[(batch - 1) % len(tokens)]
    t = desk.asset_for(sym)
    amount = int(usd / float(t["price"]) * 10**t["decimals"])
    created = []
    for h in hours:
        try:
            created.append(desk.create_order(
                origin_asset=t["assetId"], amount_in=amount, recipient=recipient, hours=h,
                soft_bps=SOFT_BPS, hard_bps=HARD_BPS, mode="shadow",
                source=f"{PREFIX}{batch}:{sym}:{h:g}", note=f"evidence series, batch {batch}"))
        except Exception:
            log.exception("series batch %s: %s %sh not started", batch, sym, h)
    if created:
        log.info("series batch %s: %s $%s at %s h", batch, sym, f"{usd:,.0f}", ", ".join(f"{h:g}" for h in hours))
    return created
