"""Dashboard: orders, slices, the agent's decisions and the TCA report.

  streamlit run ui/app.py

Reads the same database as the worker. Anyone with the link can start a SHADOW order
(dry quotes only, nothing is signed or sent); live orders are created from the CLI only.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import altair as alt
import pandas as pd
import streamlit as st
from sqlmodel import Session, select

from desk.config import AURORA, RHEA, USDC_NEAR, WNEAR, load_settings
from desk.db.models import (FILLED, ParentOrder, QuoteSnapshot, Slice, TickLog, aware, init_db,
                            make_engine)
from desk.tca.metrics import build_report

BLUE, ORANGE, GRAY = "#2a78d6", "#eb6834", "#8a8984"
X_TIME = dict(title="time, UTC", scale=alt.Scale(type="utc"), axis=alt.Axis(format="%H:%M"))
T_TIP = alt.Tooltip("utchoursminutesseconds(time):T", title="time, UTC", format="%H:%M:%S")


def txt(v, fmt="{:,.2f}"):
    return "" if v is None else fmt.format(v)
SYMBOLS = {RHEA: "RHEA", AURORA: "AURORA", WNEAR: "wNEAR", USDC_NEAR: "USDC"}
MAX_ACTIVE_SHADOW = 3
MAX_ORDER_USD = 50_000
DEMO_TREASURY = "treasury.sputnik-dao.near"

st.set_page_config(page_title="Intents Execution Desk", layout="wide")


@st.cache_resource
def get_engine():
    engine = make_engine(load_settings().database_url)
    init_db(engine)
    return engine


def sym(asset: str) -> str:
    return SYMBOLS.get(asset, asset.split(":")[-1][:16])


def load(order_id: int):
    with Session(get_engine()) as s:
        o = s.get(ParentOrder, order_id)
        slices = list(s.exec(select(Slice).where(Slice.order_id == order_id).order_by(Slice.seq)))
        ticks = list(s.exec(select(TickLog).where(TickLog.order_id == order_id).order_by(TickLog.ts)))
        curve = list(s.exec(select(QuoteSnapshot).where(QuoteSnapshot.order_id == order_id,
                                                        QuoteSnapshot.kind == "curve")))
    return o, slices, ticks, curve


# ---------------- header & order picker ----------------
st.title("Intents Execution Desk")
st.caption("Sells a DAO treasury position in small slices through NEAR Intents (1Click), "
           "waits for liquidity to recover between slices, and reports the cost against one big swap.")

with Session(get_engine()) as s:
    orders = list(s.exec(select(ParentOrder).order_by(ParentOrder.id.desc()).limit(30)))

with st.sidebar:
    st.subheader("Orders")
    if orders:
        labels = {o.id: f"#{o.id} · {sym(o.origin_asset)} · "
                        f"${int(o.total_in) / 10**o.origin_decimals * (o.arrival_mid or 0):,.0f} · {o.mode} · {o.status}"
                  for o in orders}
        picked = st.radio("Order", list(labels), format_func=labels.get, label_visibility="collapsed")
    else:
        picked = None
        st.info("No orders yet. Start a shadow order below.")
    auto = st.toggle("Auto-refresh every 20 s", value=True)


def kpis(o: ParentOrder, r) -> None:
    c1, c2, c3, c4 = st.columns(4)
    note = dict(delta_color="off", delta_arrow="off", border=True)
    c1.metric("Sold", f"{r.progress:.0%}", f"{r.slices} slices", **note)
    c2.metric(f"Received, {sym(o.destination_asset)}", f"{r.received:,.0f}",
              f"avg price {r.avg_price:.5f}" if r.avg_price else "no fills yet", **note)
    c3.metric("Implementation shortfall", "—" if r.is_bps is None else f"{r.is_bps:.0f} bps",
              "after the first slice" if r.is_bps is None
              else f"timing {r.timing_bps:.0f} + execution {r.execution_bps:.0f}", **note)
    if r.oneshot_bps is None:
        c4.metric("One swap at start", "impossible", "no liquidity for the whole size", **note)
    elif r.savings_bps is not None:
        c4.metric("Saved vs one swap", f"{r.savings_bps:.0f} bps",
                  f"≈ ${r.savings_usd:,.0f}; one swap cost {r.oneshot_bps:.0f} bps", **note)
    else:
        c4.metric("One swap at start", f"{r.oneshot_bps:.0f} bps", "cost vs arrival price", **note)


def price_chart(o: ParentOrder, slices, ticks) -> alt.Chart | None:
    mids = pd.DataFrame([{"time": aware(t.ts), "price": t.mid} for t in ticks if t.mid])
    fills = pd.DataFrame([{
        "time": aware(x.filled_at),
        "price": int(x.executed_out) / 10**o.destination_decimals / (int(x.amount_in) / 10**o.origin_decimals),
        "slice": x.seq, "cost_bps": round(x.cost_bps or 0, 1),
        "size": int(x.amount_in) / 10**o.origin_decimals,
    } for x in slices if x.status in FILLED and x.executed_out and x.filled_at])
    if mids.empty and fills.empty:
        return None
    layers = []
    color = alt.Color("series:N", scale=alt.Scale(domain=["Benchmark", "Slice fill", "Arrival price"],
                                                  range=[BLUE, ORANGE, GRAY]),
                      legend=alt.Legend(orient="top", title=None))
    y = alt.Y("price:Q", title=f"{sym(o.destination_asset)} per {sym(o.origin_asset)}", scale=alt.Scale(zero=False))
    if not mids.empty:
        mids["series"] = "Benchmark"
        layers.append(alt.Chart(mids).mark_line(strokeWidth=2).encode(
            x=alt.X("time:T", **X_TIME), y=y, color=color,
            tooltip=[T_TIP, alt.Tooltip("price:Q", format=".6f", title="benchmark")]))
    if o.arrival_mid:
        layers.append(alt.Chart(pd.DataFrame({"price": [o.arrival_mid], "series": ["Arrival price"]})).mark_rule(
            strokeDash=[4, 4], strokeWidth=1.5).encode(y="price:Q", color=color,
                                                      tooltip=[alt.Tooltip("price:Q", format=".6f", title="arrival")]))
    if not fills.empty:
        fills["series"] = "Slice fill"
        layers.append(alt.Chart(fills).mark_point(filled=True, size=90, opacity=1, stroke="white", strokeWidth=2).encode(
            x=alt.X("time:T", **X_TIME), y=y, color=color,
            tooltip=["slice:Q", T_TIP, alt.Tooltip("price:Q", format=".6f"),
                     alt.Tooltip("size:Q", format=",.0f", title="tokens"), alt.Tooltip("cost_bps:Q", title="cost, bps")]))
    return alt.layer(*layers).properties(height=300)


def schedule_chart(o: ParentOrder, ticks) -> alt.Chart | None:
    total = int(o.total_in)
    rows = []
    for t in ticks:
        rows.append({"time": aware(t.ts), "series": "Schedule", "share": int(t.target_in) / total})
        rows.append({"time": aware(t.ts), "series": "Sold", "share": int(t.done_in) / total})
    if not rows:
        return None
    df = pd.DataFrame(rows)
    return alt.Chart(df).mark_line(strokeWidth=2, interpolate="step-after").encode(
        x=alt.X("time:T", **X_TIME),
        y=alt.Y("share:Q", title="share of order", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
        color=alt.Color("series:N", scale=alt.Scale(domain=["Schedule", "Sold"], range=[GRAY, BLUE]),
                        legend=alt.Legend(orient="top", title=None)),
        strokeDash=alt.StrokeDash("series:N", scale=alt.Scale(domain=["Schedule", "Sold"], range=[[4, 4], [1, 0]]),
                                  legend=None),
        tooltip=["series:N", T_TIP, alt.Tooltip("share:Q", format=".0%")],
    ).properties(height=300)


def tca_chart(r) -> alt.Chart | None:
    if r.is_bps is None:
        return None
    rows = [{"part": "Timing (market moved)", "bps": r.timing_bps, "kind": "agent"},
            {"part": "Execution (spread, impact, fees)", "bps": r.execution_bps, "kind": "agent"},
            {"part": "Agent total (IS)", "bps": r.is_bps, "kind": "agent"}]
    if r.worst_case_is_bps is not None:
        rows.append({"part": "Shadow worst case", "bps": r.worst_case_is_bps, "kind": "reference"})
    if r.oneshot_bps is not None:
        rows.append({"part": "One swap at start", "bps": r.oneshot_bps, "kind": "reference"})
    df = pd.DataFrame(rows)
    order = list(df["part"])
    bars = alt.Chart(df).mark_bar(cornerRadiusEnd=4, height=20).encode(
        y=alt.Y("part:N", sort=order, title=None, axis=alt.Axis(labelLimit=260, labelOverlap=False)),
        x=alt.X("bps:Q", title="cost vs arrival price, bps"),
        color=alt.Color("kind:N", scale=alt.Scale(domain=["agent", "reference"], range=[BLUE, GRAY]), legend=None),
        tooltip=["part:N", alt.Tooltip("bps:Q", format=".1f")])
    labels = bars.mark_text(align="left", dx=4).encode(text=alt.Text("bps:Q", format=".0f"), color=alt.value(None))
    return (bars + labels.encode(color=alt.value("#8a8984"))).properties(height=52 * len(rows))


def curve_chart(o: ParentOrder, curve) -> alt.Chart | None:
    pts = []
    for q in curve:
        if not q.amount_out or not q.mid:
            continue
        price = int(q.amount_out) / 10**o.destination_decimals / (int(q.amount_in) / 10**o.origin_decimals)
        pts.append({"size": q.usd_size, "bps": (q.mid - price) / q.mid * 1e4})
    if not pts:
        return None
    df = pd.DataFrame(pts).sort_values("size")
    base = alt.Chart(df).encode(x=alt.X("size:Q", title="swap size, $", scale=alt.Scale(type="log")),
                                y=alt.Y("bps:Q", title="cost vs benchmark, bps"),
                                tooltip=[alt.Tooltip("size:Q", format="$,.0f"), alt.Tooltip("bps:Q", format=".0f")])
    rule = alt.Chart(pd.DataFrame({"size": [o.slice_usd]})).mark_rule(color=ORANGE, strokeDash=[4, 4]).encode(
        x="size:Q", tooltip=[alt.Tooltip("size:Q", format="$,.0f", title="chosen slice")])
    return (base.mark_line(color=BLUE, strokeWidth=2) + base.mark_point(color=BLUE, filled=True, size=70) + rule
            ).properties(height=260)


def show(order_id: int) -> None:
    o, slices, ticks, curve = load(order_id)
    if o is None:
        st.warning("Order not found.")
        return
    r = build_report(o, slices)
    total_tokens = int(o.total_in) / 10**o.origin_decimals
    st.subheader(f"Order #{o.id}: sell {total_tokens:,.0f} {sym(o.origin_asset)} → {sym(o.destination_asset)}")
    st.caption(f"{o.mode.upper()} · {o.status} · to `{o.recipient}` · deadline {aware(o.deadline_at):%Y-%m-%d %H:%M} UTC · "
               f"slices ≈ ${o.slice_usd:,.0f} · soft {o.soft_limit_bps:.0f} / hard {o.hard_limit_bps:.0f} bps"
               + (f" · {o.note}" if o.note else ""))
    if o.mode == "shadow":
        st.info("Shadow mode: every decision uses real 1Click dry quotes, but nothing is signed or sent. "
                "Fills are recorded at the dry quote; the worst case assumes liquidity never recovers between slices.",
                icon="ℹ️")
    kpis(o, r)
    st.markdown(f"**Agent now:** {o.last_action or 'waiting for the first tick'}")

    left, right = st.columns(2)
    with left:
        st.markdown("**Price: benchmark and fills**")
        ch = price_chart(o, slices, ticks)
        if ch:
            st.altair_chart(ch, use_container_width=True)
        else:
            st.caption("No data yet.")
    with right:
        st.markdown("**Schedule vs sold**")
        ch = schedule_chart(o, ticks)
        if ch:
            st.altair_chart(ch, use_container_width=True)
        else:
            st.caption("No data yet.")

    left, right = st.columns(2)
    with left:
        st.markdown("**Transaction cost analysis**")
        ch = tca_chart(r)
        if ch:
            st.altair_chart(ch, use_container_width=True)
        else:
            st.caption("Appears after the first slice.")
        if r.worst_case_is_bps is None and o.mode == "shadow" and r.slices:
            st.caption("Shadow worst case not shown: the cumulative size has no liquidity as a single swap.")
        if r.median_recovery_s is not None:
            st.caption(f"Median liquidity recovery after a slice: {r.median_recovery_s:.0f} s")
    with right:
        st.markdown("**Impact curve when the order was created**")
        ch = curve_chart(o, curve)
        if ch:
            st.altair_chart(ch, use_container_width=True)
        else:
            st.caption("No curve recorded.")
        st.caption("Dashed line: the slice size the agent chose.")

    st.markdown("**Slices**")
    if slices:
        st.dataframe(pd.DataFrame([{
            "#": x.seq, "status": x.status,
            "time, UTC": aware(x.filled_at or x.created_at).strftime("%H:%M:%S"),
            f"{sym(o.origin_asset)} sold": txt(int(x.amount_in) / 10**o.origin_decimals),
            f"{sym(o.destination_asset)} received": txt(int(x.executed_out) / 10**o.destination_decimals if x.executed_out else None),
            "cost, bps": txt(x.cost_bps, "{:.1f}"),
            "worst case": txt(int(x.shadow_worst_out) / 10**o.destination_decimals if x.shadow_worst_out else None)
                          if o.mode == "shadow" else "",
            "recovery, s": txt(x.recovery_s, "{:.0f}"), "note": x.skip_reason or "",
        } for x in reversed(slices)]), hide_index=True, use_container_width=True)
    else:
        st.caption("No slices yet.")

    st.markdown("**Decision log**")
    if ticks:
        st.dataframe(pd.DataFrame([{
            "time, UTC": aware(t.ts).strftime("%H:%M:%S"), "action": t.action, "reason": t.reason,
            "next-slice cost, bps": txt(t.cost_bps, "{:.1f}"),
        } for t in reversed(ticks[-200:])]), hide_index=True, use_container_width=True, height=280)


@st.fragment(run_every=20 if auto else None)
def live_view():
    if picked is not None:
        show(picked)


live_view()

# ---------------- new shadow order ----------------
st.divider()
with st.expander("Start a shadow order (free: dry quotes only, nothing is signed or sent)"):
    with Session(get_engine()) as s:
        active = sum(1 for o in s.exec(select(ParentOrder).where(ParentOrder.status == "ACTIVE",
                                                                  ParentOrder.mode == "shadow")))
    if active >= MAX_ACTIVE_SHADOW:
        st.warning(f"{active} shadow orders are already running; try again when one finishes.")
    else:
        with st.form("new_order"):
            c1, c2, c3 = st.columns(3)
            token = c1.selectbox("Sell", ["RHEA", "AURORA", "wNEAR"])
            usd = c2.number_input("Size, $", min_value=500, max_value=MAX_ORDER_USD, value=25_000, step=500)
            hours = c3.number_input("Horizon, hours", min_value=0.5, max_value=6.0, value=2.0, step=0.5)
            c4, c5, c6 = st.columns(3)
            soft = c4.number_input("Soft limit, bps", min_value=10, max_value=1000, value=150, step=10)
            hard = c5.number_input("Hard limit, bps", min_value=20, max_value=2000, value=250, step=10)
            to = c6.text_input("Treasury account (receives USDC)", value=DEMO_TREASURY)
            go = st.form_submit_button("Start shadow order")
        if go:
            if hard <= soft:
                st.error("Hard limit must be above the soft limit.")
            else:
                from desk.cli import find_token
                from desk.worker import build_desk
                with st.spinner("Measuring the impact curve with dry quotes..."):
                    desk = build_desk(load_settings())
                    t = find_token(desk.client.tokens(), token)
                    amount = int(usd / float(t["price"]) * 10**t["decimals"])
                    o = desk.create_order(origin_asset=t["assetId"], amount_in=amount, recipient=to.strip(),
                                          hours=float(hours), soft_bps=float(soft), hard_bps=float(hard),
                                          mode="shadow", note="started from the dashboard")
                st.success(f"Order #{o.id} started. The worker picks it up within 20 seconds.")
                st.rerun()
