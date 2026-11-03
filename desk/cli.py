"""Command line helpers.

  python -m desk.cli curve RHEA          benchmark micro-mid + impact curve (dry quotes, free)
  python -m desk.cli new-key             generate an agent key and print its implicit account
"""

from __future__ import annotations

import argparse
import sys

from desk.config import USDC_NEAR, load_settings
from desk.near.signer import AgentSigner
from desk.oneclick.client import OneClickClient
from desk.prices.benchmark import TokenInfo, build_curve

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
    k = sub.add_parser("new-key", help="generate an agent signing key")
    k.set_defaults(func=cmd_new_key)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
