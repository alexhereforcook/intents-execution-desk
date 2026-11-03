"""A fake 1Click backed by a constant-product pool, for engine tests without network."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from desk.config import USDC_NEAR
from desk.oneclick.client import NoLiquidity, Quote

TOKEN = "nep141:token.test.near"
FEE = 0.002


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


class FakeOneClick:
    """x*y=k pool. Live swaps move the reserves; arbitrage pulls them back with a half-life."""

    def __init__(self, clock: Clock, *, reserve_token=3_000_000.0, reserve_usd=195_000.0,
                 recovery_half_life_s=120.0, max_usd=30_000.0):
        self.clock = clock
        self.base = (reserve_token, reserve_usd)
        self.r_token, self.r_usd = reserve_token, reserve_usd
        self.half_life = recovery_half_life_s
        self.max_usd = max_usd
        self.last_t = clock()
        self.live_quote_haircut = 0.0      # set >0 to return a bad live quote
        self.deposits: dict[str, dict] = {}
        self.submitted: list[dict] = []
        self.generated = 0

    # pool mechanics
    def _recover(self):
        dt = (self.clock() - self.last_t).total_seconds()
        self.last_t = self.clock()
        if dt <= 0 or self.half_life <= 0:
            return
        w = 0.5 ** (dt / self.half_life)
        self.r_token = self.base[0] + (self.r_token - self.base[0]) * w
        self.r_usd = self.base[1] + (self.r_usd - self.base[1]) * w

    def _out(self, qty: float) -> float:
        return self.r_usd * qty / (self.r_token + qty) * (1 - FEE)

    @property
    def price(self):
        return self.r_usd / self.r_token

    # API surface used by the desk
    def tokens(self):
        return [{"assetId": TOKEN, "decimals": 18, "price": self.base[1] / self.base[0], "symbol": "TST",
                 "blockchain": "near"},
                {"assetId": USDC_NEAR, "decimals": 6, "price": 1.0, "symbol": "USDC", "blockchain": "near"}]

    def quote(self, *, origin_asset, destination_asset, amount_in, account, recipient=None,
              dry=True, slippage_bps=100, deadline_s=600):
        self._recover()
        qty = amount_in / 1e18
        if qty * self.price > self.max_usd:
            raise NoLiquidity(400, "No liquidity available")
        out = self._out(qty) * (1 - (0 if dry else self.live_quote_haircut))
        out_units = int(out * 1e6)
        dep = None
        if not dry:
            dep = f"dep{len(self.deposits) + 1}"
            self.deposits[dep] = {"qty": qty, "out": out_units, "status": "PENDING_DEPOSIT"}
        return Quote(amount_in=amount_in, amount_out=out_units, min_amount_out=None, time_estimate_s=10,
                     deposit_address=dep, deposit_memo=None, raw={})

    def generate_intent(self, *, deposit_address, signer_id, deposit_memo=None):
        self.generated += 1
        import base64
        import json
        return {"standard": "nep413", "payload": {
            "recipient": "intents.near", "nonce": base64.b64encode(bytes(32)).decode(),
            "message": json.dumps({"signer_id": signer_id, "deposit": deposit_address})}}

    def submit_intent(self, signed):
        import json
        dep = json.loads(signed["payload"]["message"])["deposit"]
        self._recover()
        d = self.deposits[dep]
        out = self._out(d["qty"])          # executes at the pool price now
        self.r_usd -= out / (1 - FEE)
        self.r_token += d["qty"]
        d.update(status="SUCCESS", out=int(out * 1e6))
        self.submitted.append(signed)
        return f"hash-{dep}"

    def status(self, deposit_address, deposit_memo=None):
        d = self.deposits.get(deposit_address)
        if d is None:
            return {"status": "PENDING_DEPOSIT"}
        res = {"status": d["status"]}
        if d["status"] == "SUCCESS":
            res["swapDetails"] = {"amountOut": str(d["out"])}
        return res


def isclose(a, b, rel=1e-6):
    return math.isclose(a, b, rel_tol=rel)
