import base64
import json

import httpx
import pytest
from sqlmodel import Session, select

from desk.dao.sputnik import MandateError, approved_mandates, parse_mandate
from desk.db.models import ParentOrder
from desk.near.rpc import NearRpc, RpcError
from tests.fakes import TOKEN
from tests.test_engine import setup

DAO = "alexdesk.sputnikv2.testnet"
AGENT = "a" * 64
MANDATE = 'Sell treasury RHEA for runway\nexecution-desk: {"sell": "RHEA", "usd": 25000, "hours": 2, "soft": 150, "hard": 250}'


def proposal(pid, status="Approved", kind="Vote", desc=MANDATE):
    return {"id": pid, "proposer": "alex.testnet", "description": desc, "kind": kind, "status": status,
            "vote_counts": {}, "votes": {}, "submission_time": "0"}


def test_vote_mandate_is_a_shadow_order_paid_to_the_dao():
    m = parse_mandate(DAO, "testnet", proposal(3), AGENT)
    assert m.source == f"sputnik:testnet:{DAO}#3"
    assert (m.sell, m.usd, m.hours, m.soft, m.hard) == ("RHEA", 25000, 2, 150, 250)
    assert m.recipient == DAO and not m.funded


def test_plain_proposals_are_ignored_and_bad_mandates_explained():
    assert parse_mandate(DAO, "testnet", proposal(1, desc="Pay the designer"), AGENT) is None
    with pytest.raises(MandateError, match="JSON"):
        parse_mandate(DAO, "testnet", proposal(2, desc="execution-desk: {sell: RHEA}"), AGENT)
    with pytest.raises(MandateError, match="soft"):
        parse_mandate(DAO, "testnet", proposal(2, desc='execution-desk: {"sell":"RHEA","usd":1000,"soft":300,"hard":200}'), AGENT)
    with pytest.raises(MandateError, match="capped"):
        parse_mandate(DAO, "testnet", proposal(2, desc='execution-desk: {"sell":"RHEA","usd":5000000}'), AGENT)
    with pytest.raises(MandateError, match="kind"):
        parse_mandate(DAO, "testnet", proposal(2, kind={"AddMemberToRole": {}}), AGENT)


def test_transfer_mandate_funds_the_agent_on_mainnet_only():
    kind = {"Transfer": {"token_id": "token.rhealab.near", "receiver_id": "intents.near",
                         "amount": str(10**22), "msg": AGENT}}
    desc = 'execution-desk: {"hours": 4, "soft": 120, "hard": 220}'
    m = parse_mandate("x.sputnik-dao.near", "mainnet", proposal(7, kind=kind, desc=desc), AGENT)
    assert m.funded and m.amount_units == 10**22 and m.sell == "nep141:token.rhealab.near"
    with pytest.raises(MandateError, match="mainnet"):
        parse_mandate(DAO, "testnet", proposal(7, kind=kind, desc=desc), AGENT)
    wrong = {"Transfer": {**kind["Transfer"], "msg": "someone.near"}}
    with pytest.raises(MandateError, match="agent account"):
        parse_mandate("x.sputnik-dao.near", "mainnet", proposal(7, kind=wrong, desc=desc), AGENT)


def rpc_with(proposals):
    def handler(request):
        params = json.loads(request.content)["params"]
        method = params["method_name"]
        if method == "get_last_proposal_id":
            out = len(proposals)
        elif method == "get_proposals":
            args = json.loads(base64.b64decode(params["args_base64"]))
            out = proposals[args["from_index"]:args["from_index"] + args["limit"]]
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "desk", "result": {"error": "MethodNotFound"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "desk",
                                         "result": {"result": list(json.dumps(out).encode())}})
    return NearRpc("testnet", transport=httpx.MockTransport(handler))


def test_only_approved_mandates_are_returned():
    rpc = rpc_with([proposal(0, desc="hello"), proposal(1, status="InProgress"), proposal(2),
                    proposal(3, desc='execution-desk: {"sell": "RHEA"}')])
    found, skipped = approved_mandates(rpc, DAO, AGENT)
    assert [m.proposal_id for m in found] == [2]
    assert len(skipped) == 1 and "#3" in skipped[0]


def test_rpc_errors_surface():
    with pytest.raises(RpcError):
        rpc_with([]).view(DAO, "no_such_method")


def test_mandates_become_orders_once(tmp_path):
    clock, fake, engine, desk, _ = setup(tmp_path)
    desc = f'execution-desk: {{"sell": "{TOKEN}", "usd": 5000, "hours": 1, "soft": 150, "hard": 300}}'
    m = parse_mandate(DAO, "testnet", proposal(4, desc=desc), desk.account)
    first = desk.import_mandates([m])
    again = desk.import_mandates([m])
    assert len(first) == 1 and again == []
    with Session(engine) as s:
        o = s.exec(select(ParentOrder).where(ParentOrder.source == m.source)).one()
    assert o.recipient == DAO and o.mode == "shadow" and "proposal #4" in o.note
    assert abs(int(o.total_in) / 1e18 * fake.tokens()[0]["price"] - 5000) < 1
