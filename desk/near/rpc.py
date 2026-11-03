"""Read-only NEAR RPC: view calls on contracts (no keys, no transactions)."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from desk.config import USER_AGENT

RPC_URLS = {
    "mainnet": ["https://rpc.mainnet.near.org", "https://rpc.mainnet.fastnear.com"],
    "testnet": ["https://rpc.testnet.near.org", "https://rpc.testnet.fastnear.com"],
}


class RpcError(RuntimeError):
    pass


class NearRpc:
    def __init__(self, network: str = "mainnet", timeout: float = 20.0,
                 transport: httpx.BaseTransport | None = None):
        if network not in RPC_URLS:
            raise ValueError(f"unknown NEAR network {network!r}")
        self.network = network
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, transport=transport)

    def view(self, account_id: str, method: str, args: dict | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": "desk", "method": "query",
                "params": {"request_type": "call_function", "finality": "final",
                           "account_id": account_id, "method_name": method,
                           "args_base64": base64.b64encode(json.dumps(args or {}).encode()).decode()}}
        last: Exception | None = None
        for url in RPC_URLS[self.network]:
            try:
                resp = self._http.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                last = e
                continue
            if "error" in data:
                raise RpcError(f"{method} on {account_id}: {json.dumps(data['error'])[:300]}")
            result = data.get("result", {})
            if result.get("error"):
                raise RpcError(f"{method} on {account_id}: {result['error'][:300]}")
            return json.loads(bytes(result["result"]).decode())
        raise RpcError(f"no {self.network} RPC answered: {last}")
