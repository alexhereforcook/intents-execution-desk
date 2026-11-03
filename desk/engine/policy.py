"""Tick decision: execute the next slice now, or wait.

Adaptive TWAP. The schedule is linear but ends a bit before the deadline
(SCHEDULE_END_FRACTION) to leave room for skipped ticks. Inside the schedule the agent
prefers moments when the quote is close to the benchmark, and after each slice it waits
until the quote for the next slice comes back (liquidity recovered) before trading again.

Pure functions only: no I/O, easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SCHEDULE_END_FRACTION = 0.9
MIN_GAP_S = 60                 # never two slices closer than this
RECOVERY_TOL_BPS = 10          # "recovered" = cost back within this of the pre-trade cost


@dataclass(frozen=True)
class Limits:
    soft_bps: float            # good enough to trade when on or ahead of schedule
    hard_bps: float            # never trade worse than this


@dataclass(frozen=True)
class TickState:
    now: datetime
    start: datetime
    deadline: datetime
    total_in: int
    done_in: int               # filled (or shadow-filled)
    slice_in: int
    cost_bps: float            # cost of the next slice vs benchmark, from a fresh dry quote
    last_fill_at: datetime | None = None
    last_fill_cost_bps: float | None = None    # cost that last slice had right before it traded
    in_flight: bool = False


@dataclass(frozen=True)
class Decision:
    action: str                # EXECUTE | WAIT | FINISH
    reason: str
    amount_in: int = 0
    recovered: bool | None = None


def target_in(s: TickState) -> int:
    """Amount that should be sold by now on the linear schedule."""
    horizon = (s.deadline - s.start).total_seconds() * SCHEDULE_END_FRACTION
    if horizon <= 0:
        return s.total_in
    frac = min(max((s.now - s.start).total_seconds() / horizon, 0.0), 1.0)
    return int(s.total_in * frac)


def is_recovered(s: TickState) -> bool | None:
    if s.last_fill_at is None or s.last_fill_cost_bps is None:
        return None
    return s.cost_bps <= s.last_fill_cost_bps + RECOVERY_TOL_BPS


def decide(s: TickState, lim: Limits) -> Decision:
    remaining = s.total_in - s.done_in
    if remaining <= 0:
        return Decision("FINISH", "fully sold")
    if s.now >= s.deadline:
        return Decision("FINISH", "deadline reached, remainder returns to the treasury")
    if s.in_flight:
        return Decision("WAIT", "previous slice still settling")

    amount = min(s.slice_in, remaining)
    behind = target_in(s) - s.done_in
    recovered = is_recovered(s)

    if s.cost_bps > lim.hard_bps:
        return Decision("WAIT", f"quote {s.cost_bps:.0f} bps worse than hard limit {lim.hard_bps:.0f}",
                        recovered=recovered)

    since_last = (s.now - s.last_fill_at).total_seconds() if s.last_fill_at else None
    if since_last is not None and since_last < MIN_GAP_S:
        return Decision("WAIT", f"{since_last:.0f}s since last slice, min gap {MIN_GAP_S}s",
                        recovered=recovered)

    # well behind schedule: trade anything within the hard limit
    if behind >= s.slice_in:
        return Decision("EXECUTE", f"behind schedule by {behind / s.slice_in:.1f} slices",
                        amount, recovered)

    if recovered is False:
        return Decision("WAIT", "liquidity not recovered since last slice", recovered=recovered)

    # A good quote is taken only when the order is not ahead of schedule. Allowing a
    # slice of slack let the first live run (Nov 3) sell 80% of a 2-hour order in 46 min.
    if behind <= 0:
        return Decision("WAIT", "ahead of schedule", recovered=recovered)
    if s.cost_bps <= lim.soft_bps:
        return Decision("EXECUTE", f"good quote: {s.cost_bps:.0f} bps <= soft {lim.soft_bps:.0f}",
                        amount, recovered)
    return Decision("WAIT", f"quote {s.cost_bps:.0f} bps, waiting for better (soft {lim.soft_bps:.0f})",
                    recovered=recovered)
