import json

import httpx
import pytest
import respx

from desk.config import RHEA, USDC_NEAR, USER_AGENT
from desk.oneclick.client import NoLiquidity, OneClickClient, OneClickError

BASE = "https://1click.chaindefuser.com/v0"
ACCOUNT = "a" * 64


@pytest.fixture
def client():
    c = OneClickClient("test-key")
    yield c
    c.close()


@respx.mock
def test_quote_request_shape_and_parsing(client):
    route = respx.post(f"{BASE}/quote").mock(return_value=httpx.Response(200, json={
        "quote": {"amountIn": "1000000000000000000000", "amountOut": "63650000",
                  "minAmountOut": "63000000", "timeEstimate": 12}}))
    q = client.quote(origin_asset=RHEA, destination_asset=USDC_NEAR,
                     amount_in=1000 * 10**18, account=ACCOUNT, recipient="dao.sputnik-dao.near")
    req = route.calls.last.request
    body = json.loads(req.content)
    assert req.headers["Authorization"] == "Bearer test-key"
    assert req.headers["User-Agent"] == USER_AGENT
    assert body["dry"] is True
    assert body["depositType"] == body["refundType"] == "INTENTS"
    assert body["recipient"] == "dao.sputnik-dao.near" and body["refundTo"] == ACCOUNT
    assert body["amount"] == str(1000 * 10**18)
    assert q.amount_out == 63_650_000 and q.time_estimate_s == 12
    assert q.price(18, 6) == pytest.approx(0.06365)


@respx.mock
def test_no_liquidity_is_its_own_error(client):
    respx.post(f"{BASE}/quote").mock(return_value=httpx.Response(
        400, json={"message": "No liquidity available"}))
    with pytest.raises(NoLiquidity):
        client.quote(origin_asset=RHEA, destination_asset=USDC_NEAR, amount_in=1, account=ACCOUNT)


@respx.mock
def test_other_errors(client):
    respx.post(f"{BASE}/quote").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(OneClickError) as e:
        client.quote(origin_asset=RHEA, destination_asset=USDC_NEAR, amount_in=1, account=ACCOUNT)
    assert not isinstance(e.value, NoLiquidity)
    assert e.value.status_code == 500


@respx.mock
def test_generate_and_submit_use_x_api_key(client):
    gen = respx.post(f"{BASE}/generate-intent").mock(return_value=httpx.Response(
        200, json={"intent": {"standard": "nep413", "payload": {}}, "correlationId": "x"}))
    sub = respx.post(f"{BASE}/submit-intent").mock(return_value=httpx.Response(
        200, json={"intentHash": "HASH", "correlationId": "x"}))
    intent = client.generate_intent(deposit_address="dep", signer_id=ACCOUNT, deposit_memo="memo")
    assert intent["standard"] == "nep413"
    assert gen.calls.last.request.headers["X-API-Key"] == "test-key"
    assert json.loads(gen.calls.last.request.content)["depositMemo"] == "memo"
    assert client.submit_intent({"standard": "nep413"}) == "HASH"
    assert json.loads(sub.calls.last.request.content)["type"] == "swap_transfer"


@respx.mock
def test_status_params(client):
    route = respx.get(f"{BASE}/status").mock(return_value=httpx.Response(200, json={"status": "SUCCESS"}))
    assert client.status("dep")["status"] == "SUCCESS"
    assert route.calls.last.request.url.params["depositAddress"] == "dep"
