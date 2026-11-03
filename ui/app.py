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
X_TIME = dict(title="time, UTC", scale=alt.Scale(type="utc"),
              axis=alt.Axis(format="%H:%M", tickMinStep=60_000, labelOverlap=True))
T_TIP = alt.Tooltip("utchoursminutesseconds(time):T", title="time, UTC", format="%H:%M:%S")


def txt(v, fmt="{:,.2f}"):
    return "" if v is None else fmt.format(v)
SYMBOLS = {RHEA: "RHEA", AURORA: "AURORA", WNEAR: "wNEAR", USDC_NEAR: "USDC"}
MAX_ACTIVE_SHADOW = 4
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


def dao_link(o: ParentOrder) -> str | None:
    """Markdown link to the DAO on NearBlocks for orders that came from a Sputnik proposal."""
    if not o.source or not o.source.startswith("sputnik:"):
        return None
    _, network, rest = o.source.split(":", 2)
    dao, pid = rest.split("#")
    host = "testnet.nearblocks.io" if network == "testnet" else "nearblocks.io"
    return f"approved by [{dao}](https://{host}/address/{dao}) proposal #{pid}"


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
                        + (" · DAO" if o.source else "")
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
        layers.append(alt.Chart(mids).mark_line(strokeWidth=2, interpolate="step-after").encode(
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
    df["label_x"] = df["bps"].clip(lower=0)          # labels sit right of the zero line or the bar end
    labels = alt.Chart(df).mark_text(align="left", dx=4, color=GRAY).encode(
        y=alt.Y("part:N", sort=order), x="label_x:Q", text=alt.Text("bps:Q", format=".0f"))
    return (bars + labels).properties(height=52 * len(rows))


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
               + (f" · {dao_link(o)}" if dao_link(o) else (f" · {o.note}" if o.note else "")))
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
            st.altair_chart(ch, width="stretch")
        else:
            st.caption("No data yet.")
    with right:
        st.markdown("**Schedule vs sold**")
        ch = schedule_chart(o, ticks)
        if ch:
            st.altair_chart(ch, width="stretch")
        else:
            st.caption("No data yet.")

    left, right = st.columns(2)
    with left:
        st.markdown("**Transaction cost analysis**")
        ch = tca_chart(r)
        if ch:
            st.altair_chart(ch, width="stretch")
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
            st.altair_chart(ch, width="stretch")
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
        } for x in reversed(slices)]), hide_index=True, width="stretch")
    else:
        st.caption("No slices yet.")

    st.markdown("**Decision log**")
    if ticks:
        st.dataframe(pd.DataFrame([{
            "time, UTC": aware(t.ts).strftime("%H:%M:%S"), "action": t.action, "reason": t.reason,
            "next-slice cost, bps": txt(t.cost_bps, "{:.1f}"),
        } for t in reversed(ticks[-200:])]), hide_index=True, width="stretch", height=280)


def results_table() -> None:
    """Every finished order in one table: the evidence across tokens, sizes and horizons."""
    with Session(get_engine()) as s:
        done = list(s.exec(select(ParentOrder).where(ParentOrder.status == "DONE").order_by(ParentOrder.id)))
        rows = []
        for o in done:
            r = build_report(o, list(s.exec(select(Slice).where(Slice.order_id == o.id))))
            hours = (aware(o.deadline_at) - aware(o.created_at)).total_seconds() / 3600
            usd = int(o.total_in) / 10**o.origin_decimals * (o.arrival_mid or 0)
            rows.append({
                "order": f"#{o.id}", "mode": o.mode, "source": "DAO proposal" if o.source else "dashboard / CLI",
                "token": sym(o.origin_asset),
                "size, $": f"{usd:,.0f}", "horizon, h": f"{hours:g}", "slices": r.slices,
                "slice, $": f"{o.slice_usd:,.0f}",
                "execution, bps": txt(r.execution_bps, "{:.0f}"),
                "timing, bps": txt(r.timing_bps, "{:.0f}"),
                "IS, bps": txt(r.is_bps, "{:.0f}"),
                "one swap at start, bps": "no liquidity" if r.oneshot_bps is None else f"{r.oneshot_bps:.0f}",
                "saved vs one swap, bps": txt(r.savings_bps, "{:.0f}"),
                "shadow worst case, bps": txt(r.worst_case_is_bps, "{:.0f}"),
            })
    if not rows:
        st.caption("No finished orders yet.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Execution = cost of the slices vs the benchmark at each slice (what the agent controls). "
               "Timing = how the market moved during the order. IS = execution + timing, vs the price at the start.")


@st.fragment(run_every=20 if auto else None)
def live_view():
    if picked is not None:
        show(picked)


tab_order, tab_results = st.tabs(["Order", "Results across orders"])
with tab_order:
    live_view()
with tab_results:
    results_table()

# ---------------- new shadow order ----------------
AQUA = "#1baf7a"
HORIZON_CHOICES = [0.5, 1.0, 2.0, 4.0, 6.0]


def horizon_label(h: float) -> str:
    return "one swap now" if h == 0 else f"{h:g} h"


def pretrade_chart(p) -> alt.Chart:
    rows = []
    for r in p.rows:
        if r.execution_bps is None:
            continue
        lbl = horizon_label(r.hours)
        rows += [{"horizon": lbl, "series": "Execution cost", "bps": r.execution_bps},
                 {"horizon": lbl, "series": "Timing risk (1σ)", "bps": r.risk_bps},
                 {"horizon": lbl, "series": "Cost + risk", "bps": r.score_bps}]
    df = pd.DataFrame(rows)
    order = [horizon_label(r.hours) for r in p.rows if r.execution_bps is not None]
    color = alt.Color("series:N", scale=alt.Scale(domain=["Execution cost", "Timing risk (1σ)", "Cost + risk"],
                                                  range=[BLUE, ORANGE, AQUA]),
                      legend=alt.Legend(orient="top", title=None))
    base = alt.Chart(df).encode(x=alt.X("horizon:N", sort=order, title="time to sell", axis=alt.Axis(labelAngle=0)),
                                y=alt.Y("bps:Q", title="bps of order value"), color=color,
                                tooltip=["horizon:N", "series:N", alt.Tooltip("bps:Q", format=".0f")])
    layers = [base.mark_line(strokeWidth=2), base.mark_point(filled=True, size=70)]
    if p.recommended_hours is not None:
        rec = pd.DataFrame({"horizon": [horizon_label(p.recommended_hours)]})
        layers.append(alt.Chart(rec).mark_rule(color=GRAY, strokeDash=[4, 4]).encode(x=alt.X("horizon:N", sort=order)))
    return alt.layer(*layers).properties(height=280)


st.divider()
with st.expander("Start a shadow order (free: dry quotes only, nothing is signed or sent)", expanded=False):
    with Session(get_engine()) as s:
        active = sum(1 for o in s.exec(select(ParentOrder).where(ParentOrder.status == "ACTIVE",
                                                                  ParentOrder.mode == "shadow")))
    if active >= MAX_ACTIVE_SHADOW:
        st.warning(f"{active} shadow orders are already running; try again when one finishes.")
    else:
        st.markdown("**1. What to sell**")
        c1, c2, c3, c4 = st.columns(4)
        token = c1.selectbox("Sell", ["RHEA", "AURORA", "wNEAR"])
        usd = c2.number_input("Size, $", min_value=500, max_value=MAX_ORDER_USD, value=25_000, step=500)
        soft = c3.number_input("Soft limit, bps", min_value=10, max_value=1000, value=150, step=10)
        hard = c4.number_input("Hard limit, bps", min_value=20, max_value=2000, value=250, step=10)
        to = st.text_input("Treasury account (receives USDC)", value=DEMO_TREASURY)
        key = (token, usd, soft)

        if st.button("Analyze: how long should we take?"):
            from desk.cli import find_token
            from desk.worker import build_desk
            with st.spinner("Quoting the impact curve and reading volatility..."):
                desk = build_desk(load_settings())
                t = find_token(desk.client.tokens(), token)
                amount = int(usd / float(t["price"]) * 10**t["decimals"])
                st.session_state["pretrade"] = (key, desk.pretrade(origin_asset=t["assetId"], amount_in=amount,
                                                                   soft_bps=float(soft)), t["assetId"], amount)

        saved = st.session_state.get("pretrade")
        if saved and saved[0] == key:
            _, p, asset_id, amount = saved
            st.markdown("**2. Pre-trade analysis** (Almgren–Chriss trade-off)")
            st.altair_chart(pretrade_chart(p), width="stretch")
            rec = p.recommended_hours
            if p.oneshot_bps is None:
                st.caption(f"One swap of ${usd:,.0f} right now: **no liquidity**.")
            if rec == 0:
                st.success(f"{token} is liquid enough: one swap now costs ~{p.oneshot_bps:.0f} bps and beats any "
                           "schedule once timing risk is counted. The agent is not needed for this order.")
            elif rec is not None:
                row = next(r for r in p.rows if r.hours == rec)
                st.success(f"Recommended: **{horizon_label(rec)}** in ~${row.slice_usd:,.0f} slices — "
                           f"execution ~{row.execution_bps:.0f} bps, timing risk ±{row.risk_bps:.0f} bps (1σ).")
            st.caption(f"Volatility: {p.sigma_note}. Execution cost is read from the impact curve measured "
                       "just now at the slice size each horizon needs; timing risk is σ·√(T/3) for a linear schedule.")

            st.markdown("**3. Start**")
            default = rec if rec in HORIZON_CHOICES else 2.0
            hours = st.selectbox("Horizon", HORIZON_CHOICES, index=HORIZON_CHOICES.index(default),
                                 format_func=horizon_label)
            if st.button("Start shadow order", type="primary"):
                if hard <= soft:
                    st.error("Hard limit must be above the soft limit.")
                else:
                    from desk.worker import build_desk
                    with st.spinner("Creating the order..."):
                        o = build_desk(load_settings()).create_order(
                            origin_asset=asset_id, amount_in=amount, recipient=to.strip(), hours=float(hours),
                            soft_bps=float(soft), hard_bps=float(hard), mode="shadow",
                            note="started from the dashboard")
                    st.session_state.pop("pretrade", None)
                    st.success(f"Order #{o.id} started. The worker picks it up within 20 seconds.")
                    st.rerun()
