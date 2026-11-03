"""Checks a live (executable) quote before the agent signs it.

On Oct 24, 2026 a live quote for 1,000 RHEA came back at 27.91 USDC while dry quotes
for the same size said ~63: 56% worse. slippageTolerance does not protect against that,
because it is measured from the quote itself. So the agent compares every live quote
with a fresh dry quote and with the benchmark, and refuses to sign if either is off.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_LIVE_VS_DRY_BPS = 30       # live may be at most this much worse than the dry quote


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reason: str
    live_cost_bps: float
    live_vs_dry_bps: float


def check_live_quote(*, live_out: int, dry_out: int, amount_in: int, in_decimals: int,
                     out_decimals: int, mid: float, hard_bps: float,
                     max_vs_dry_bps: float = MAX_LIVE_VS_DRY_BPS) -> GuardResult:
    qty = amount_in / 10**in_decimals
    live_price = (live_out / 10**out_decimals) / qty
    live_cost = (mid - live_price) / mid * 1e4
    vs_dry = (dry_out - live_out) / dry_out * 1e4 if dry_out else float("inf")
    if vs_dry > max_vs_dry_bps:
        return GuardResult(False, f"live quote {vs_dry:.0f} bps worse than dry quote", live_cost, vs_dry)
    if live_cost > hard_bps:
        return GuardResult(False, f"live quote {live_cost:.0f} bps vs benchmark > hard {hard_bps:.0f}",
                           live_cost, vs_dry)
    return GuardResult(True, "ok", live_cost, vs_dry)
