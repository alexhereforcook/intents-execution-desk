"""The always-on loop: ticks every active order, forever.

State lives in the database, so the worker can be restarted at any time: in-flight
slices are reconciled through GET /v0/status, never sent twice.
"""

from __future__ import annotations

import logging
import time

from sqlmodel import Session

from desk.config import Settings
from desk.db.models import active_orders, init_db, make_engine
from desk.engine.desk import ExecutionDesk
from desk.near.signer import AgentSigner
from desk.oneclick.client import OneClickClient

log = logging.getLogger("desk.worker")
TICK_S = 20


def build_desk(settings: Settings) -> ExecutionDesk:
    engine = make_engine(settings.database_url)
    init_db(engine)
    signer = AgentSigner.from_hex(settings.agent_seed_hex) if settings.agent_seed_hex else None
    return ExecutionDesk(engine, OneClickClient(settings.oneclick_api_key), signer=signer)


def run_forever(settings: Settings, every_s: float = TICK_S) -> None:
    desk = build_desk(settings)
    log.info("worker started, mode=%s, agent=%s", settings.mode, desk.account)
    while True:
        with Session(desk.engine) as s:
            ids = [o.id for o in active_orders(s)]
        for oid in ids:
            try:
                d = desk.tick(oid)
                log.info("order %s: %s %s", oid, d.action, d.reason)
            except Exception:  # keep the loop alive; the DB state is the source of truth
                log.exception("order %s: tick failed", oid)
        time.sleep(every_s)
