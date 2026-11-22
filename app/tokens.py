"""One-time payment authorization tokens.

Format:  mpt1.<base64url(payload JSON)>.<base64url(Ed25519 signature over the payload part)>

Payload binds the token to one mandate, one action, one merchant and one amount, with a short
expiry. Tokens are issued only by the policy server (on allow, or when the owner approves) and are
the ONLY thing the mock wallet accepts as authority to pay.
"""
import base64
import json
import uuid
from datetime import timedelta

from cryptography.exceptions import InvalidSignature

from . import clock, crypto

PREFIX = "mpt1"
TOKEN_TTL = timedelta(minutes=10)
TOKEN_TYPE = "mandate.payment_authorization"


class TokenError(Exception):
    """Token is malformed or its signature does not verify."""


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(conn, *, mandate_id: str, action_id: str, merchant: str, amount: float,
          currency: str) -> dict:
    """Create, sign and store a token. Caller holds db.write_lock and commits."""
    now = clock.now()
    payload = {
        "typ": TOKEN_TYPE,
        "jti": "tok_" + uuid.uuid4().hex,
        "mandate_id": mandate_id,
        "action_id": action_id,
        "merchant": merchant,
        "amount": round(float(amount), 2),
        "currency": currency,
        "iat": clock.iso(now),
        "exp": clock.iso(now + TOKEN_TTL),
    }
    body = _b64e(crypto.canonical_json(payload).encode())
    sig = _b64e(crypto.private_key().sign(body.encode()))
    token = f"{PREFIX}.{body}.{sig}"
    conn.execute(
        "INSERT INTO payment_tokens (jti, token, mandate_id, action_id, merchant, amount, currency, "
        "issued_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (payload["jti"], token, mandate_id, action_id, merchant, payload["amount"], currency,
         payload["iat"], payload["exp"]),
    )
    return {"token": token, "expires_at": payload["exp"], "jti": payload["jti"]}


def verify(token: str) -> dict:
    """Return the payload if the token is well-formed and signed by this server; else raise."""
    if not isinstance(token, str) or token.count(".") != 2:
        raise TokenError("malformed token")
    prefix, body, sig = token.split(".")
    if prefix != PREFIX:
        raise TokenError("unknown token version")
    try:
        crypto.public_key().verify(_b64d(sig), body.encode())
    except (InvalidSignature, ValueError):
        raise TokenError("invalid token signature (forged or altered)")
    try:
        payload = json.loads(_b64d(body))
    except ValueError:
        raise TokenError("malformed token payload")
    if payload.get("typ") != TOKEN_TYPE:
        raise TokenError("wrong token type")
    return payload


def for_action(conn, action_id: str) -> dict | None:
    row = conn.execute("SELECT token, expires_at, used_at FROM payment_tokens WHERE action_id=? "
                       "ORDER BY issued_at DESC LIMIT 1", (action_id,)).fetchone()
    return dict(row) if row else None
