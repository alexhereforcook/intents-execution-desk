"""Orders that come from approved Sputnik DAO proposals.

The DAO votes on the mandate; the agent only executes what was approved.

A proposal is a mandate when its description contains a line like

  execution-desk: {"sell": "RHEA", "usd": 25000, "hours": 2, "soft": 150, "hard": 250}

("amount" in whole tokens can replace "usd"; "to" can override the account that
receives USDC, which defaults to the DAO itself). Two proposal kinds are accepted:

  * Vote (a signalling proposal): a mandate without funds. The order runs in SHADOW
    mode. This is how the demo works, and it needs no tokens at all.
  * Transfer of a NEP-141 token to intents.near with msg = the agent's account:
    approving it deposits the tokens straight into the agent's NEAR Intents balance
    (ft_transfer_call), so the vote both authorises and funds the order. The size is
    the transferred amount; the order runs LIVE when the desk is in live mode.

Only proposals with status Approved are executed, each one once.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from desk.near.rpc import NearRpc

MARKER = re.compile(r"execution-desk\s*:\s*(\{.*?\})", re.DOTALL)
INTENTS_CONTRACT = "intents.near"
MAX_SHADOW_USD = 50_000


class MandateError(ValueError):
    pass


@dataclass(frozen=True)
class Mandate:
    dao: str
    proposal_id: int
    source: str                 # "sputnik:<network>:<dao>#<id>": unique key of the order
    sell: str                   # symbol or asset id
    usd: float | None
    amount_tokens: float | None
    amount_units: int | None    # exact smallest units (Transfer proposals)
    hours: float
    soft: float
    hard: float
    recipient: str
    funded: bool                # True for Transfer proposals that deposit into the agent's balance
    description: str


def parse_mandate(dao: str, network: str, proposal: dict, agent_account: str) -> Mandate | None:
    """Mandate from one proposal (as returned by get_proposals), or None if it is not one.

    Raises MandateError when the proposal is meant for the desk but is malformed, so the
    worker can log why it was skipped instead of silently ignoring it.
    """
    desc = proposal.get("description") or ""
    m = MARKER.search(desc)
    if not m:
        return None
    try:
        params = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise MandateError(f"proposal #{proposal.get('id')}: mandate is not valid JSON: {e}") from None

    kind = proposal.get("kind")
    funded, units, sell = False, None, params.get("sell")
    if kind == "Vote":
        pass
    elif isinstance(kind, dict) and "Transfer" in kind:
        t = kind["Transfer"]
        if network != "mainnet":
            raise MandateError("funded mandates (Transfer) only make sense on mainnet")
        if t.get("receiver_id") != INTENTS_CONTRACT or t.get("msg") != agent_account:
            raise MandateError(f"transfer must go to {INTENTS_CONTRACT} with msg = agent account {agent_account}")
        if not t.get("token_id"):
            raise MandateError("native NEAR transfers are not supported; send a NEP-141 token")
        funded, units, sell = True, int(t["amount"]), f"nep141:{t['token_id']}"
    else:
        raise MandateError(f"proposal #{proposal.get('id')}: kind {kind!r} cannot carry a mandate")

    if not sell:
        raise MandateError("mandate needs 'sell'")
    usd = params.get("usd")
    amount = params.get("amount")
    if not funded and (usd is None) == (amount is None):
        raise MandateError("mandate needs exactly one of 'usd' or 'amount'")
    hours = float(params.get("hours", 2))
    soft = float(params.get("soft", 150))
    hard = float(params.get("hard", 250))
    if not 0.25 <= hours <= 24:
        raise MandateError("hours must be between 0.25 and 24")
    if not 0 < soft < hard <= 2000:
        raise MandateError("need 0 < soft < hard <= 2000 bps")
    if usd is not None and not funded and float(usd) > MAX_SHADOW_USD:
        raise MandateError(f"shadow mandates are capped at ${MAX_SHADOW_USD:,}")

    pid = int(proposal["id"])
    return Mandate(dao=dao, proposal_id=pid, source=f"sputnik:{network}:{dao}#{pid}", sell=str(sell),
                   usd=None if usd is None else float(usd),
                   amount_tokens=None if amount is None else float(amount), amount_units=units,
                   hours=hours, soft=soft, hard=hard, recipient=str(params.get("to") or dao),
                   funded=funded, description=desc)


def approved_mandates(rpc: NearRpc, dao: str, agent_account: str, scan_last: int = 50
                      ) -> tuple[list[Mandate], list[str]]:
    """Approved mandates among the DAO's last `scan_last` proposals, plus skip reasons."""
    last = int(rpc.view(dao, "get_last_proposal_id"))
    start = max(0, last - scan_last)
    proposals = rpc.view(dao, "get_proposals", {"from_index": start, "limit": scan_last}) if last else []
    found, skipped = [], []
    for p in proposals:
        if p.get("status") != "Approved":
            continue
        try:
            m = parse_mandate(dao, rpc.network, p, agent_account)
        except MandateError as e:
            skipped.append(f"{dao}#{p.get('id')}: {e}")
            continue
        if m:
            found.append(m)
    return found, skipped
