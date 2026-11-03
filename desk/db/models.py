"""Persistent state: parent orders, their slices, and every quote the agent saw.

Token amounts are stored as decimal strings in smallest units: 18-decimal tokens
overflow BIGINT. Use the helpers to read them as int.

Slice status moves forward only:
  PLANNED -> QUOTED -> SIGNED -> SUBMITTED -> SUCCESS | REFUNDED | FAILED
  PLANNED -> SKIPPED            (guard rejected the quote, or deadline reached)
  QUOTED  -> SHADOW_FILLED      (shadow mode: recorded at the dry quote, nothing signed)
The status is written BEFORE each external call, so after a restart the worker
reconciles SIGNED / SUBMITTED slices via GET /v0/status instead of sending them again.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Field, Session, SQLModel, create_engine, select


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ParentOrder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=utcnow)
    mode: str = "shadow"                    # shadow | live
    status: str = "ACTIVE"                  # ACTIVE | PAUSED | DONE | CANCELLED

    origin_asset: str
    origin_decimals: int
    destination_asset: str
    destination_decimals: int
    total_in: str                           # smallest units of origin
    recipient: str                          # DAO treasury account (receives the output)

    deadline_at: datetime
    slice_usd: float                        # target slice size
    hard_limit_bps: float                   # never sell a slice worse than this vs benchmark
    soft_limit_bps: float                   # preferred quality when ahead of schedule

    arrival_mid: float | None = None        # benchmark at start (micro-mid)
    oneshot_out: str | None = None          # dry quote for the whole size at start
    note: str | None = None                 # e.g. Sputnik DAO proposal link

    def total_in_int(self) -> int:
        return int(self.total_in)


class Slice(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="parentorder.id", index=True)
    seq: int
    status: str = "PLANNED"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    amount_in: str
    mid_at_quote: float | None = None
    quoted_out: str | None = None           # live (or shadow dry) quote amountOut
    executed_out: str | None = None         # from status / balance after SUCCESS
    deposit_address: str | None = None
    deposit_memo: str | None = None
    intent_hash: str | None = None
    skip_reason: str | None = None
    recovery_s: float | None = None         # time for quotes to come back after previous slice

    def amount_in_int(self) -> int:
        return int(self.amount_in)


class QuoteSnapshot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    order_id: int | None = Field(default=None, foreign_key="parentorder.id", index=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    kind: str                               # micro | curve | oneshot | slice | recovery
    asset: str
    usd_size: float | None = None
    amount_in: str
    amount_out: str | None = None           # None = no liquidity
    mid: float | None = None


def make_engine(database_url: str):
    # Neon / Supabase hand out postgres:// or postgresql:// URLs; use the psycopg 3 driver.
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
    kwargs = {"connect_args": {"check_same_thread": False}} if database_url.startswith("sqlite") else {"pool_pre_ping": True}
    return create_engine(database_url, **kwargs)


def init_db(engine) -> None:
    SQLModel.metadata.create_all(engine)


def active_orders(session: Session) -> list[ParentOrder]:
    return list(session.exec(select(ParentOrder).where(ParentOrder.status == "ACTIVE")))


def unsettled_slices(session: Session, order_id: int) -> list[Slice]:
    """Slices that were sent out but have no final status yet: reconcile, never resend."""
    return list(session.exec(select(Slice).where(Slice.order_id == order_id,
                                                 Slice.status.in_(["SIGNED", "SUBMITTED"]))))
