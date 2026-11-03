"""Transaction cost analysis for a sell order (all figures in bps of the arrival price).

  IS        = (P_arr - P_exec) / P_arr
  timing    = (P_arr - P_mid)  / P_arr     the market moved while we were selling
  execution = (P_mid - P_exec) / P_arr     spread, impact and fees vs the benchmark
  IS = timing + execution exactly.

P_arr  = benchmark (micro-mid) when the order was created
P_exec = output received / tokens sold
P_mid  = benchmark at each slice, weighted by slice size
One-shot = dry quote for the whole order at creation time.

On thin markets our own sells move the benchmark, so part of "timing" is our own
footprint. The report says so; splitting temporary and permanent impact is future work.
"""

from __future__ import annotations

from dataclasses import dataclass

from desk.db.models import FILLED, ParentOrder, Slice


@dataclass(frozen=True)
class Report:
    sold_tokens: float
    total_tokens: float
    received: float
    slices: int
    arrival_mid: float
    avg_price: float | None
    is_bps: float | None
    timing_bps: float | None
    execution_bps: float | None
    oneshot_bps: float | None          # None = no liquidity for the whole size at once
    savings_bps: float | None
    savings_usd: float | None
    worst_case_is_bps: float | None    # shadow only: if liquidity never recovered
    median_recovery_s: float | None    # live only

    @property
    def progress(self) -> float:
        return self.sold_tokens / self.total_tokens if self.total_tokens else 0.0


def _bps(ref: float, price: float) -> float:
    return (ref - price) / ref * 1e4


def build_report(order: ParentOrder, slices: list[Slice]) -> Report:
    din, dout = 10**order.origin_decimals, 10**order.destination_decimals
    filled = [s for s in slices if s.status in FILLED and s.executed_out]
    qty = sum(int(s.amount_in) for s in filled) / din
    out = sum(int(s.executed_out) for s in filled) / dout
    total = int(order.total_in) / din
    p_arr = order.arrival_mid or 0.0

    oneshot_bps = None
    if order.oneshot_out and p_arr:
        oneshot_bps = _bps(p_arr, int(order.oneshot_out) / dout / total)

    if not filled or not p_arr:
        return Report(qty, total, out, len(filled), p_arr, None, None, None, None,
                      oneshot_bps, None, None, None, None)

    p_exec = out / qty
    weights = [(int(s.amount_in) / din, s.mid_at_quote or p_arr) for s in filled]
    p_mid = sum(q * m for q, m in weights) / qty
    is_bps = _bps(p_arr, p_exec)
    savings_bps = oneshot_bps - is_bps if oneshot_bps is not None else None
    savings_usd = savings_bps / 1e4 * p_arr * qty if savings_bps is not None else None

    worst = None
    if order.mode == "shadow" and all(s.shadow_worst_out for s in filled):
        worst = _bps(p_arr, sum(int(s.shadow_worst_out) for s in filled) / dout / qty)

    rec = sorted(s.recovery_s for s in filled if s.recovery_s is not None)
    median_rec = rec[len(rec) // 2] if rec else None

    return Report(qty, total, out, len(filled), p_arr, p_exec, is_bps, _bps(p_arr, p_mid),
                  (p_mid - p_exec) / p_arr * 1e4, oneshot_bps, savings_bps, savings_usd, worst, median_rec)
