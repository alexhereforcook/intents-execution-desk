"""The execution desk: creates parent orders and runs one tick at a time.

Shadow mode (default): every decision is made on real 1Click dry quotes, fills are
recorded at the dry quote, nothing is signed or sent. Because the agent's own sells
never hit the pool in shadow mode, each shadow slice also stores a worst case:
the output if liquidity never recovered between slices,
  worst_k = Q(sold_before + slice_k) - Q(sold_before),
where Q is a dry quote for a cumulative amount. Real execution lands between the two.

Live mode: live quote -> guard -> generate-intent -> sign (NEP-413) -> submit-intent
-> poll /status. The slice status is committed before each external step, so a restart
reconciles in-flight slices instead of sending them again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlmodel import Session

from desk.config import USDC_NEAR
from desk.db.models import (FILLED, IN_FLIGHT, ParentOrder, QuoteSnapshot, Slice, aware,
                            order_slices, utcnow)
from desk.engine.guard import check_live_quote
from desk.engine.policy import SCHEDULE_END_FRACTION, Decision, Limits, TickState, decide
from desk.near.signer import AgentSigner
from desk.oneclick.client import FINAL_STATUSES, NoLiquidity, OneClickClient, OneClickError
from desk.prices.benchmark import (MICRO_SIZES_USD, TokenInfo, build_curve, impact_bps,
                                   micro_mid, sample)

BENCHMARK_TTL_S = 120
LIVE_QUOTE_DEADLINE_S = 180
SHADOW_ACCOUNT = "0" * 64      # dry quotes need an account id; nothing is sent
TARGET_SLICE_SPACING_S = 300   # aim for roughly one slice every 5 minutes


def choose_slice_usd(order_usd: float, hours: float, curve, soft_bps: float) -> float:
    """Smallest slice that still fits the horizon, never above what the soft limit allows.

    Smaller slices cost less (RHEA, Nov 3: $1k ~42 bps, $5k ~127 bps), as long as liquidity
    recovers between them; the horizon caps how many slices there is time for.
    """
    max_slices = max(1, int(hours * 3600 * SCHEDULE_END_FRACTION / TARGET_SLICE_SPACING_S))
    smallest = min(p.usd_size for p in curve.points)
    size = max(order_usd / max_slices, smallest)
    cap = curve.max_size_within(soft_bps)
    return min(size, cap) if cap else smallest


@dataclass
class _Mid:
    value: float
    at: datetime


class ExecutionDesk:
    def __init__(self, engine, client: OneClickClient, *, signer: AgentSigner | None = None,
                 clock: Callable[[], datetime] = utcnow):
        self.engine = engine
        self.client = client
        self.signer = signer
        self.clock = clock
        self._mids: dict[int, _Mid] = {}
        self._tokens: dict[str, dict] | None = None

    # ---------- helpers ----------
    @property
    def account(self) -> str:
        return self.signer.account_id if self.signer else SHADOW_ACCOUNT

    def token(self, asset_id: str) -> dict:
        if self._tokens is None or asset_id not in self._tokens:
            self._tokens = {t["assetId"]: t for t in self.client.tokens()}
        return self._tokens[asset_id]

    def _dry_out(self, o: ParentOrder, amount_in: int) -> int:
        return self.client.quote(origin_asset=o.origin_asset, destination_asset=o.destination_asset,
                                 amount_in=amount_in, account=self.account, recipient=o.recipient).amount_out

    def _log_quote(self, s: Session, o: ParentOrder, kind: str, amount_in: int,
                   amount_out: int | None, mid: float | None, usd: float | None = None) -> None:
        s.add(QuoteSnapshot(order_id=o.id, kind=kind, asset=o.origin_asset, usd_size=usd,
                            amount_in=str(amount_in), amount_out=None if amount_out is None else str(amount_out),
                            mid=mid, ts=self.clock()))

    def _price(self, o: ParentOrder, amount_in: int, amount_out: int) -> float:
        return (amount_out / 10**o.destination_decimals) / (amount_in / 10**o.origin_decimals)

    def _mid(self, s: Session, o: ParentOrder) -> float:
        now = self.clock()
        cached = self._mids.get(o.id)
        if cached and (now - cached.at).total_seconds() < BENCHMARK_TTL_S:
            return cached.value
        t = self.token(o.origin_asset)
        info = TokenInfo(o.origin_asset, o.origin_decimals, float(t["price"]))
        pts = sample(info, o.destination_decimals, MICRO_SIZES_USD, lambda a: self._dry_out(o, a))
        mid = micro_mid(pts)
        for p in pts:
            self._log_quote(s, o, "micro", int(p.token_qty * 10**o.origin_decimals), None, mid, p.usd_size)
        self._mids[o.id] = _Mid(mid, now)
        return mid

    # ---------- orders ----------
    def create_order(self, *, origin_asset: str, amount_in: int, recipient: str, hours: float,
                     soft_bps: float, hard_bps: float, slice_usd: float | None = None,
                     destination_asset: str = USDC_NEAR, mode: str = "shadow",
                     note: str | None = None) -> ParentOrder:
        if mode == "live" and self.signer is None:
            raise ValueError("live mode needs AGENT_SEED_HEX")
        t_in, t_out = self.token(origin_asset), self.token(destination_asset)
        info = TokenInfo(origin_asset, t_in["decimals"], float(t_in["price"]))
        stub = ParentOrder(origin_asset=origin_asset, origin_decimals=t_in["decimals"],
                           destination_asset=destination_asset, destination_decimals=t_out["decimals"],
                           total_in=str(amount_in), recipient=recipient, deadline_at=self.clock(),
                           slice_usd=0, hard_limit_bps=hard_bps, soft_limit_bps=soft_bps)
        curve = build_curve(info, t_out["decimals"], lambda a: self._dry_out(stub, a))
        if slice_usd is None:
            order_usd = amount_in / 10**t_in["decimals"] * curve.mid
            slice_usd = choose_slice_usd(order_usd, hours, curve, soft_bps)
        try:
            oneshot = self._dry_out(stub, amount_in)
        except NoLiquidity:
            oneshot = None

        now = self.clock()
        slice_in = int(slice_usd / curve.mid * 10**t_in["decimals"])
        o = ParentOrder(mode=mode, origin_asset=origin_asset, origin_decimals=t_in["decimals"],
                        destination_asset=destination_asset, destination_decimals=t_out["decimals"],
                        total_in=str(amount_in), recipient=recipient, created_at=now,
                        deadline_at=now + timedelta(hours=hours), slice_usd=slice_usd,
                        slice_in=str(min(slice_in, amount_in)), hard_limit_bps=hard_bps,
                        soft_limit_bps=soft_bps, arrival_mid=curve.mid,
                        oneshot_out=None if oneshot is None else str(oneshot), note=note)
        with Session(self.engine) as s:
            s.add(o)
            s.commit()
            s.refresh(o)
            for p in curve.points:
                self._log_quote(s, o, "curve", int(p.token_qty * 10**o.origin_decimals), None, curve.mid, p.usd_size)
            self._log_quote(s, o, "oneshot", amount_in, oneshot, curve.mid)
            s.commit()
            s.refresh(o)
            self._mids[o.id] = _Mid(curve.mid, now)
            return o

    # ---------- one tick ----------
    def tick(self, order_id: int) -> Decision:
        with Session(self.engine) as s:
            o = s.get(ParentOrder, order_id)
            if o is None or o.status != "ACTIVE":
                return Decision("WAIT", "order not active")
            self._reconcile(s, o)
            slices = order_slices(s, o.id)
            done_in = sum(x.amount_in_int() for x in slices if x.status in FILLED)
            in_flight = any(x.status in IN_FLIGHT for x in slices)
            filled = [x for x in slices if x.status in FILLED]
            last = filled[-1] if filled else None
            remaining = o.total_in_int() - done_in
            amount = min(o.slice_in_int(), remaining)

            cost, dry_out, mid = float("inf"), None, None
            if remaining > 0 and not in_flight:
                try:
                    mid = self._mid(s, o)
                    dry_out = self._dry_out(o, amount)
                    cost = impact_bps(self._price(o, amount, dry_out), mid)
                    self._log_quote(s, o, "slice", amount, dry_out, mid)
                except NoLiquidity:
                    self._log_quote(s, o, "slice", amount, None, mid)

            state = TickState(now=self.clock(), start=aware(o.created_at), deadline=aware(o.deadline_at),
                              total_in=o.total_in_int(), done_in=done_in, slice_in=o.slice_in_int(),
                              cost_bps=cost, last_fill_at=aware(last.filled_at) if last else None,
                              last_fill_cost_bps=last.cost_bps if last else None, in_flight=in_flight)
            d = decide(state, Limits(o.soft_limit_bps, o.hard_limit_bps))

            # in shadow mode our sells never hit the pool, so "recovery" would be meaningless
            if o.mode == "live" and d.recovered and last is not None and last.recovery_s is None:
                last.recovery_s = (state.now - aware(last.filled_at)).total_seconds()
                s.add(last)

            if d.action == "FINISH":
                o.status, o.finished_at = "DONE", state.now
            elif d.action == "EXECUTE":
                sl = Slice(order_id=o.id, seq=len(slices) + 1, amount_in=str(d.amount_in),
                           mid_at_quote=mid, cost_bps=cost, dry_out=str(dry_out),
                           created_at=state.now, updated_at=state.now)
                if o.mode == "shadow":
                    self._shadow_fill(o, sl, done_in, dry_out)
                else:
                    s.add(sl)
                    s.commit()
                    s.refresh(sl)
                    self._live_execute(s, o, sl, dry_out, mid)
                s.add(sl)
            o.last_action = f"{d.action}: {d.reason}"
            s.add(o)
            s.commit()
            return d

    # ---------- shadow ----------
    def _shadow_fill(self, o: ParentOrder, sl: Slice, sold_before: int, dry_out: int) -> None:
        sl.status, sl.executed_out, sl.filled_at = "SHADOW_FILLED", str(dry_out), self.clock()
        try:
            before = self._dry_out(o, sold_before) if sold_before > 0 else 0
            after = self._dry_out(o, sold_before + sl.amount_in_int())
            sl.shadow_worst_out = str(max(after - before, 0))
        except NoLiquidity:
            sl.shadow_worst_out = None   # cumulative size has no liquidity: worst case unknown

    # ---------- live ----------
    def _set(self, s: Session, sl: Slice, **fields) -> None:
        for k, v in fields.items():
            setattr(sl, k, v)
        sl.updated_at = self.clock()
        s.add(sl)
        s.commit()

    def _live_execute(self, s: Session, o: ParentOrder, sl: Slice, dry_out: int, mid: float) -> None:
        assert self.signer is not None
        amount = sl.amount_in_int()
        try:
            q = self.client.quote(origin_asset=o.origin_asset, destination_asset=o.destination_asset,
                                  amount_in=amount, account=self.account, recipient=o.recipient,
                                  dry=False, slippage_bps=int(o.hard_limit_bps),
                                  deadline_s=LIVE_QUOTE_DEADLINE_S)
        except OneClickError as e:
            self._set(s, sl, status="SKIPPED", skip_reason=f"live quote failed: {e.message}")
            return
        self._set(s, sl, status="QUOTED", quoted_out=str(q.amount_out), deposit_address=q.deposit_address,
                  deposit_memo=q.deposit_memo,
                  quote_deadline=self.clock() + timedelta(seconds=LIVE_QUOTE_DEADLINE_S))
        g = check_live_quote(live_out=q.amount_out, dry_out=dry_out, amount_in=amount,
                             in_decimals=o.origin_decimals, out_decimals=o.destination_decimals,
                             mid=mid, hard_bps=o.hard_limit_bps)
        if not g.ok:
            self._set(s, sl, status="SKIPPED", skip_reason=f"guard: {g.reason}")
            return
        intent = self.client.generate_intent(deposit_address=q.deposit_address, signer_id=self.account,
                                             deposit_memo=q.deposit_memo)
        signed = self.signer.sign_intent(intent)
        self._set(s, sl, status="SIGNED")
        intent_hash = self.client.submit_intent(signed)
        self._set(s, sl, status="SUBMITTED", intent_hash=intent_hash)

    def _reconcile(self, s: Session, o: ParentOrder) -> None:
        for sl in order_slices(s, o.id):
            if sl.status not in IN_FLIGHT or not sl.deposit_address:
                continue
            try:
                st = self.client.status(sl.deposit_address, sl.deposit_memo)
            except OneClickError:
                continue
            status = st.get("status")
            if status in FINAL_STATUSES:
                details = st.get("swapDetails") or {}
                out = details.get("amountOut") or sl.quoted_out
                self._set(s, sl, status=status, executed_out=str(out) if status == "SUCCESS" else None,
                          filled_at=self.clock() if status == "SUCCESS" else None)
            elif (sl.status == "SIGNED" and status == "PENDING_DEPOSIT"
                  and sl.quote_deadline and self.clock() > aware(sl.quote_deadline)):
                # signed but never submitted (crash in between) and the quote has expired
                self._set(s, sl, status="FAILED", skip_reason="signed intent was not delivered")
