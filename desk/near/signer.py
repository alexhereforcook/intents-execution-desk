"""NEP-413 signing for NEAR Intents.

Spec: https://github.com/near/NEPs/blob/master/neps/nep-0413.md
sha256( borsh(u32 2^31+413) || borsh(Payload{message, nonce[32], recipient, callbackUrl?}) ),
then ed25519 over that digest.

The agent uses an implicit NEAR account: account id = hex of the ed25519 public key.
NEAR Intents accepts it without any key registration.

Verified on Oct 24, 2026 against simulate_intents on intents.near: a valid signature
reaches the balance check; a corrupted one fails with "invalid signature".
"""

from __future__ import annotations

import base64
import hashlib
import struct

import base58
from nacl.signing import SigningKey, VerifyKey

NEP413_TAG = 2**31 + 413


def _borsh_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b


def nep413_digest(message: str, nonce_b64: str, recipient: str,
                  callback_url: str | None = None) -> bytes:
    nonce = base64.b64decode(nonce_b64)
    if len(nonce) != 32:
        raise ValueError(f"nonce must be 32 bytes, got {len(nonce)}")
    payload = _borsh_string(message) + nonce + _borsh_string(recipient)
    payload += b"\x00" if callback_url is None else b"\x01" + _borsh_string(callback_url)
    return hashlib.sha256(struct.pack("<I", NEP413_TAG) + payload).digest()


class AgentSigner:
    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError("ed25519 seed must be 32 bytes")
        self._sk = SigningKey(seed)

    @classmethod
    def from_hex(cls, seed_hex: str) -> "AgentSigner":
        return cls(bytes.fromhex(seed_hex))

    @classmethod
    def generate(cls) -> "AgentSigner":
        return cls(bytes(SigningKey.generate()))

    @property
    def seed_hex(self) -> str:
        return bytes(self._sk).hex()

    @property
    def account_id(self) -> str:
        """Implicit NEAR account id (64 hex chars)."""
        return bytes(self._sk.verify_key).hex()

    @property
    def public_key(self) -> str:
        return "ed25519:" + base58.b58encode(bytes(self._sk.verify_key)).decode()

    def sign_intent(self, intent: dict) -> dict:
        """Return the MultiPayload for submit-intent: the intent plus public_key and signature.

        The payload must not be modified between generate-intent and signing.
        """
        if intent.get("standard") != "nep413":
            raise ValueError(f"only nep413 is supported, got {intent.get('standard')!r}")
        p = intent["payload"]
        digest = nep413_digest(p["message"], p["nonce"], p["recipient"], p.get("callbackUrl"))
        sig = self._sk.sign(digest).signature
        VerifyKey(bytes(self._sk.verify_key)).verify(digest, sig)
        return {**intent, "public_key": self.public_key,
                "signature": "ed25519:" + base58.b58encode(sig).decode()}
