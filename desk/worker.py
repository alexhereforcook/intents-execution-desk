"""The always-on loop: ticks every active order, forever.

State lives in the database, so the worker can be restarted at any time: in-flight
slices are reconciled through GET /v0/status, never sent twice.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import httpx

from sqlmodel import Session

from desk.config import AURORA, RHEA, WNEAR, Settings
from desk.db.models import active_orders, init_db, make_engine
from desk.engine.desk import ExecutionDesk
from desk.near.signer import AgentSigner
from desk.oneclick.client import OneClickClient

log = logging.getLogger("desk.worker")
TICK_S = 20
WATCH_EVERY_S = 300
WATCH_ASSETS = [RHEA, AURORA, WNEAR]
DAO_EVERY_S = 60
SELF_PING_EVERY_S = 240


def _self_ping_forever(url: str) -> None:
    """Render's free web service sleeps after 15 minutes without inbound requests, and the
    worker sleeps with it. The GitHub Actions ping alone proved unreliable (scheduled runs
    are often late), so the service also requests its own public URL every 4 minutes."""
    while True:
        time.sleep(SELF_PING_EVERY_S)
        try:
            httpx.get(f"{url.rstrip('/')}/_stcore/health", timeout=30)
        except httpx.HTTPError as e:
            log.warning("self-ping failed: %s", e)


def start_self_ping() -> None:
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("DESK_URL")
    if url:
        threading.Thread(target=_self_ping_forever, args=(url,), daemon=True).start()
        log.info("self-ping every %ss: %s", SELF_PING_EVERY_S, url)


def build_desk(settings: Settings) -> ExecutionDesk:
    engine = make_engine(settings.database_url)
    init_db(engine)
    signer = AgentSigner.from_hex(settings.agent_seed_hex) if settings.agent_seed_hex else None
    return ExecutionDesk(engine, OneClickClient(settings.oneclick_api_key), signer=signer)


def run_forever(settings: Settings, every_s: float = TICK_S) -> None:
    desk = build_desk(settings)
    log.info("worker started, mode=%s, agent=%s", settings.mode, desk.account)
    start_self_ping()
    last_watch = last_dao = 0.0
    rpc = None
    seen_skips: set[str] = set()
    if settings.daos:
        from desk.near.rpc import NearRpc
        rpc = NearRpc(settings.dao_network)
        log.info("watching Sputnik DAOs on %s: %s", settings.dao_network, ", ".join(settings.daos))
    while True:
        if rpc and time.time() - last_dao >= DAO_EVERY_S:
            last_dao = time.time()
            from desk.dao.sputnik import approved_mandates
            for dao in settings.daos:
                try:
                    found, skipped = approved_mandates(rpc, dao, desk.account)
                    for reason in skipped:
                        if reason not in seen_skips:
                            seen_skips.add(reason)
                            log.warning("mandate skipped: %s", reason)
                    for o in desk.import_mandates(found, live_allowed=settings.is_live):
                        log.info("order %s created from %s", o.id, o.source)
                except Exception:
                    log.exception("DAO %s: reading proposals failed", dao)
        if time.time() - last_watch >= WATCH_EVERY_S:
            last_watch = time.time()
            try:
                desk.watch_market(WATCH_ASSETS)
            except Exception:
                log.exception("market watch failed")
        with Session(desk.engine) as s:
            ids = [o.id for o in active_orders(s)]
        for oid in ids:
            try:
                d = desk.tick(oid)
                log.info("order %s: %s %s", oid, d.action, d.reason)
            except Exception:  # keep the loop alive; the DB state is the source of truth
                log.exception("order %s: tick failed", oid)
        time.sleep(every_s)
