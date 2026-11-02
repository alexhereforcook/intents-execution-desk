"""Client for the NEAR Intents 1Click Swap API.

Docs: https://docs.near-intents.org/integration/distribution-channels/1click-api/about-1click-api

The desk always trades from the agent's NEAR Intents balance:
depositType / refundType = INTENTS, so a slice is paid by signing an intent
(generate-intent -> sign -> submit-intent), with no on-chain deposit per slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from desk.config import ONECLICK_BASE_URL, USER_AGENT

FINAL_STATUSES = {"SUCCESS", "REFUNDED", "FAILED"}


class OneClickError(RuntimeError):
    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(f"1Click HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.body = body


class NoLiquidity(OneClickError):
    """Solvers returned no quote for this size ('No liquidity available' / 'Failed to get quote')."""


@dataclass(frozen=True)
class Quote:
    amount_in: int            # origin asset, smallest units
    amount_out: int           # destination asset, smallest units
    min_amount_out: int | None
    time_estimate_s: int | None
    deposit_address: str | None   # only for non-dry quotes
    deposit_memo: str | None
    raw: dict

    def price(self, in_decimals: int, out_decimals: int) -> float:
        """Destination units per one origin unit, decimal-adjusted."""
        return (self.amount_out / 10**out_decimals) / (self.amount_in / 10**in_decimals)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class OneClickClient:
    def __init__(self, api_key: str, base_url: str = ONECLICK_BASE_URL,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        if not api_key:
            raise ValueError("ONECLICK_API_KEY is empty")
        self._key = api_key
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    # ---- low level ----
    def _request(self, method: str, path: str, *, json: dict | None = None,
                 params: dict | None = None, auth: str = "bearer") -> Any:
        headers = {}
        if auth == "bearer":
            headers["Authorization"] = f"Bearer {self._key}"
        elif auth == "x-api-key":
            headers["X-API-Key"] = self._key
        resp = self._http.request(method, path, json=json, params=params, headers=headers)
        if resp.status_code >= 400:
            try:
                body = resp.json()
                message = body.get("message") or str(body)
            except ValueError:
                body, message = resp.text, resp.text[:300]
            lowered = str(message).lower()
            if resp.status_code == 400 and ("no liquidity" in lowered or "failed to get quote" in lowered):
                raise NoLiquidity(resp.status_code, message, body)
            raise OneClickError(resp.status_code, message, body)
        return resp.json()

    # ---- endpoints ----
    def tokens(self) -> list[dict]:
        return self._request("GET", "/tokens", auth="none")

    def quote(self, *, origin_asset: str, destination_asset: str, amount_in: int,
              account: str, recipient: str | None = None, dry: bool = True,
              slippage_bps: int = 100, deadline_s: int = 600) -> Quote:
        """EXACT_INPUT quote selling `amount_in` of origin from the agent's Intents balance.

        `recipient` defaults to the agent account (INTENTS balance). For the treasury
        payout pass the DAO account and keep recipientType INTENTS.
        """
        body = {
            "dry": dry,
            "swapType": "EXACT_INPUT",
            "slippageTolerance": slippage_bps,
            "originAsset": origin_asset,
            "depositType": "INTENTS",
            "destinationAsset": destination_asset,
            "amount": str(amount_in),
            "recipient": recipient or account,
            "recipientType": "INTENTS",
            "refundTo": account,
            "refundType": "INTENTS",
            "deadline": _iso(datetime.now(timezone.utc) + timedelta(seconds=deadline_s)),
        }
        data = self._request("POST", "/quote", json=body)
        q = data["quote"]
        return Quote(
            amount_in=int(q.get("amountIn") or amount_in),
            amount_out=int(q["amountOut"]),
            min_amount_out=int(q["minAmountOut"]) if q.get("minAmountOut") else None,
            time_estimate_s=q.get("timeEstimate"),
            deposit_address=q.get("depositAddress"),
            deposit_memo=q.get("depositMemo"),
            raw=data,
        )

    def generate_intent(self, *, deposit_address: str, signer_id: str,
                        deposit_memo: str | None = None) -> dict:
        """Unsigned NEP-413 intent that moves the slice to 1Click's deposit account."""
        body = {"type": "swap_transfer", "standard": "nep413",
                "signerId": signer_id, "depositAddress": deposit_address}
        if deposit_memo:
            body["depositMemo"] = deposit_memo
        return self._request("POST", "/generate-intent", json=body, auth="x-api-key")["intent"]

    def submit_intent(self, signed_data: dict) -> str:
        data = self._request("POST", "/submit-intent", auth="x-api-key",
                             json={"type": "swap_transfer", "signedData": signed_data})
        return data["intentHash"]

    def status(self, deposit_address: str, deposit_memo: str | None = None) -> dict:
        params = {"depositAddress": deposit_address}
        if deposit_memo:
            params["depositMemo"] = deposit_memo
        return self._request("GET", "/status", params=params)
