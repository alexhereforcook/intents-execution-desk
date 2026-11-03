"""Command line.

  python -m desk.cli curve RHEA                       impact curve from dry quotes (free)
  python -m desk.cli new-order RHEA --usd 25000 --to treasury.sputnik-dao.near --hours 4
  python -m desk.cli run                              worker: ticks active orders forever
  python -m desk.cli status [ORDER_ID]                progress and TCA report
  python -m desk.cli cancel ORDER_ID                  stop an active order
  python -m desk.cli new-key                          agent signing key (live mode only)
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlmodel import Session, select

from desk.config import USDC_NEAR, load_settings
from desk.db.models import ParentOrder, order_slices
from desk.near.signer import AgentSigner
from desk.oneclick.client import OneClickClient
from desk.prices.benchmark import TokenInfo, build_curve
from desk.tca.metrics import build_report

PLACEHOLDER_ACCOUNT = "0" * 64   # dry quotes need some account; nothing is sent anywhere


def find_token(tokens: list[dict], symbol_or_id: str) -> dict:
    for t in tokens:
        if t["assetId"] == symbol_or_id:
            return t
    near = [t for t in tokens if t.get("blockchain") == "near"
            and t["symbol"].lower() == symbol_or_id.lower()]
    if not near:
        sys.exit(f"token {symbol_or_id!r} not found on NEAR")
    return near[0]


def cmd_curve(args) -> None:
    s = load_settings()
    client = OneClickClient(s.oneclick_api_key)
    try:
        tokens = client.tokens()
        t = find_token(tokens, args.token)
        usdc = next(x for x in tokens if x["assetId"] == USDC_NEAR)
        token = TokenInfo(t["assetId"], t["decimals"], float(t["price"]))

        def quote_fn(amount_in: int) -> int:
            return client.quote(origin_asset=token.asset_id, destination_asset=USDC_NEAR,
                                amount_in=amount_in, account=PLACEHOLDER_ACCOUNT).amount_out

        curve = build_curve(token, usdc["decimals"], quote_fn)
    finally:
        client.close()

    ref_gap = (token.ref_price_usd - curve.mid) / curve.mid * 1e4
    print(f"{t['symbol']}  micro-mid {curve.mid:.6g}  (reference price {token.ref_price_usd:.6g}, "
          f"{ref_gap:+.0f} bps vs micro-mid)\n")
    print(f"{'size, $':>10} | {'price':>10} | {'cost vs mid, bps':>16}")
    print("-" * 44)
    for p, (_, b) in zip(curve.points, curve.bps()):
        print(f"{p.usd_size:>10,.0f} | {p.price:>10.6g} | {b:>16.1f}")
    for limit in (150, 250):
        size = curve.max_size_within(limit)
        print(f"\nlargest slice within {limit} bps: " + (f"${size:,.0f}" if size else "none"))


def cmd_new_order(args) -> None:
    from desk.worker import build_desk
    s = load_settings()
    desk = build_desk(s)
    t = find_token(desk.client.tokens(), args.token)
    if args.amount is not None:
        amount = int(args.amount * 10**t["decimals"])
    else:
        amount = int(args.usd / float(t["price"]) * 10**t["decimals"])
    mode = "live" if args.live else "shadow"
    if mode == "live" and not s.is_live:
        sys.exit("refusing a live order: set DESK_MODE=live and AGENT_SEED_HEX in .env first")
    o = desk.create_order(origin_asset=t["assetId"], amount_in=amount, recipient=args.to,
                          hours=args.hours, soft_bps=args.soft, hard_bps=args.hard,
                          slice_usd=args.slice_usd, mode=mode, note=args.note)
    oneshot = (f"{int(o.oneshot_out) / 10**o.destination_decimals:,.2f} USDC" if o.oneshot_out
               else "no liquidity for the whole size")
    print(f"order #{o.id} ({mode}): sell {amount / 10**t['decimals']:,.2f} {t['symbol']} "
          f"in ~${o.slice_usd:,.0f} slices over {args.hours}h -> {args.to}")
    print(f"arrival micro-mid {o.arrival_mid:.6g}; one swap now would give {oneshot}")
    print("start the worker:  python -m desk.cli run")


def cmd_run(args) -> None:
    from desk.worker import run_forever
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per HTTP call is noise
    run_forever(load_settings(), every_s=args.every)


def cmd_status(args) -> None:
    from desk.worker import build_desk
    desk = build_desk(load_settings())
    with Session(desk.engine) as s:
        if args.order_id:
            orders = [s.get(ParentOrder, args.order_id)]
        else:
            orders = list(s.exec(select(ParentOrder).order_by(ParentOrder.id.desc()).limit(5)))
        for o in orders:
            if o is None:
                sys.exit("no such order")
            r = build_report(o, order_slices(s, o.id))
            print(f"\norder #{o.id} [{o.mode}, {o.status}] {r.sold_tokens:,.2f}/{r.total_tokens:,.2f} sold "
                  f"({r.progress:.0%}) in {r.slices} slices -> {r.received:,.2f} USDC")
            print(f"  last action: {o.last_action}")
            if r.is_bps is not None:
                print(f"  avg price {r.avg_price:.6g} vs arrival {r.arrival_mid:.6g}")
                print(f"  implementation shortfall {r.is_bps:.1f} bps = timing {r.timing_bps:.1f} "
                      f"+ execution {r.execution_bps:.1f}")
            if r.oneshot_bps is not None:
                print(f"  one swap at start: {r.oneshot_bps:.1f} bps"
                      + (f"; saved {r.savings_bps:.1f} bps (~${r.savings_usd:,.2f})"
                         if r.savings_bps is not None else ""))
            else:
                print("  one swap at start: no liquidity for the whole size")
            if r.worst_case_is_bps is not None:
                print(f"  shadow worst case (liquidity never recovers): {r.worst_case_is_bps:.1f} bps")
            if r.median_recovery_s is not None:
                print(f"  median liquidity recovery after a slice: {r.median_recovery_s:.0f}s")


def cmd_cancel(args) -> None:
    from desk.worker import build_desk
    desk = build_desk(load_settings())
    with Session(desk.engine) as s:
        o = s.get(ParentOrder, args.order_id)
        if o is None:
            sys.exit("no such order")
        if o.status != "ACTIVE":
            sys.exit(f"order #{o.id} is already {o.status}")
        o.status, o.last_action = "CANCELLED", "cancelled from the command line"
        s.add(o)
        s.commit()
    print(f"order #{args.order_id} cancelled; slices already sent are not affected")


def cmd_new_key(_args) -> None:
    signer = AgentSigner.generate()
    print("AGENT_SEED_HEX=" + signer.seed_hex)
    print("account:", signer.account_id)
    print("\nPut AGENT_SEED_HEX into .env (never commit it). Fund this account only for live mode.")


def main() -> None:
    p = argparse.ArgumentParser(prog="desk")
    sub = p.add_subparsers(required=True)

    c = sub.add_parser("curve", help="benchmark and impact curve for a token")
    c.add_argument("token", help="symbol on NEAR (RHEA, AURORA, wNEAR) or assetId")
    c.set_defaults(func=cmd_curve)

    n = sub.add_parser("new-order", help="create a sell order (shadow by default)")
    n.add_argument("token")
    size = n.add_mutually_exclusive_group(required=True)
    size.add_argument("--amount", type=float, help="tokens to sell")
    size.add_argument("--usd", type=float, help="dollar value to sell, at the reference price")
    n.add_argument("--to", required=True, help="DAO treasury account that receives USDC")
    n.add_argument("--hours", type=float, default=4)
    n.add_argument("--soft", type=float, default=150, help="soft limit, bps vs benchmark")
    n.add_argument("--hard", type=float, default=250, help="hard limit, bps vs benchmark")
    n.add_argument("--slice-usd", type=float, help="slice size; default from the impact curve")
    n.add_argument("--note", help="e.g. Sputnik DAO proposal link")
    n.add_argument("--live", action="store_true", help="really sign and send (needs DESK_MODE=live)")
    n.set_defaults(func=cmd_new_order)

    r = sub.add_parser("run", help="worker: tick active orders forever")
    r.add_argument("--every", type=float, default=20, help="seconds between ticks")
    r.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="progress and TCA report")
    st.add_argument("order_id", nargs="?", type=int)
    st.set_defaults(func=cmd_status)

    cn = sub.add_parser("cancel", help="stop an active order")
    cn.add_argument("order_id", type=int)
    cn.set_defaults(func=cmd_cancel)

    k = sub.add_parser("new-key", help="generate an agent signing key")
    k.set_defaults(func=cmd_new_key)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
