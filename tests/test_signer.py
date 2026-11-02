import base64
import hashlib
import json
import struct

import base58
import pytest
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from desk.near.signer import AgentSigner, nep413_digest

SEED = bytes(range(32))


def make_intent(signer: AgentSigner) -> dict:
    message = json.dumps({
        "deadline": "2026-11-10T00:00:00.000Z",
        "intents": [{"intent": "transfer", "receiver_id": "abc",
                     "tokens": {"nep141:token.rhealab.near": "1000"}}],
        "signer_id": signer.account_id,
    })
    return {"standard": "nep413",
            "payload": {"recipient": "intents.near",
                        "nonce": base64.b64encode(bytes(range(32))).decode(),
                        "message": message}}


def test_digest_layout_matches_nep413():
    msg, recipient = "hi", "myapp.com"
    nonce = bytes(range(32))
    expected = hashlib.sha256(
        struct.pack("<I", 2**31 + 413)
        + struct.pack("<I", 2) + b"hi"
        + nonce
        + struct.pack("<I", 9) + b"myapp.com"
        + b"\x00"
    ).digest()
    assert nep413_digest(msg, base64.b64encode(nonce).decode(), recipient) == expected


def test_digest_with_callback_url():
    nonce_b64 = base64.b64encode(bytes(32)).decode()
    a = nep413_digest("m", nonce_b64, "r")
    b = nep413_digest("m", nonce_b64, "r", "https://cb")
    assert a != b


def test_bad_nonce_length():
    with pytest.raises(ValueError):
        nep413_digest("m", base64.b64encode(b"short").decode(), "r")


def test_implicit_account_and_public_key():
    s = AgentSigner(SEED)
    assert len(s.account_id) == 64 and all(c in "0123456789abcdef" for c in s.account_id)
    raw = base58.b58decode(s.public_key.split(":", 1)[1])
    assert raw.hex() == s.account_id
    assert AgentSigner.from_hex(s.seed_hex).account_id == s.account_id


def test_signature_verifies_and_payload_is_untouched():
    s = AgentSigner(SEED)
    intent = make_intent(s)
    signed = s.sign_intent(intent)
    assert signed["payload"] == intent["payload"]
    assert signed["standard"] == "nep413"
    p = signed["payload"]
    digest = nep413_digest(p["message"], p["nonce"], p["recipient"])
    sig = base58.b58decode(signed["signature"].split(":", 1)[1])
    pub = base58.b58decode(signed["public_key"].split(":", 1)[1])
    VerifyKey(pub).verify(digest, sig)


def test_tampered_message_fails_verification():
    s = AgentSigner(SEED)
    signed = s.sign_intent(make_intent(s))
    p = signed["payload"]
    digest = nep413_digest(p["message"].replace("1000", "9999"), p["nonce"], p["recipient"])
    sig = base58.b58decode(signed["signature"].split(":", 1)[1])
    with pytest.raises(BadSignatureError):
        VerifyKey(bytes.fromhex(s.account_id)).verify(digest, sig)


def test_rejects_other_standards():
    s = AgentSigner(SEED)
    with pytest.raises(ValueError):
        s.sign_intent({"standard": "erc191", "payload": {}})
