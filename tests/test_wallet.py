"""Mock wallet enforcement: the only way to pay is a valid one-time token."""
import base64
import json
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import clock
from tests.conftest import TEST_SECRET


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def pay(client, token, merchant="amazon.in", amount=300):
    return client.post("/wallet/pay", json={"token": token, "merchant": merchant, "amount": amount})


def allowed_token(make_mandate, act, amount=300, merchant="amazon.in"):
    m = make_mandate()
    r = act(m["id"], amount, merchant=merchant)
    assert r["decision"] == "allow" and r["payment_token"], r
    return m, r


def audit_events(client):
    return [e["event"] for e in client.get("/audit?limit=1000").json()]


# ---- happy path -------------------------------------------------------------

def test_valid_pay_succeeds_and_debits_balance(client, make_mandate, act):
    _, r = allowed_token(make_mandate, act)
    before = client.get("/wallet").json()["balance"]
    res = pay(client, r["payment_token"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "paid" and body["simulated"] is True
    assert body["balance_after"] == before - 300
    assert client.get("/wallet").json()["recent_payments"][0]["id"] == body["payment_id"]
    assert "payment.completed" in audit_events(client)
    assert client.get("/audit/verify").json()["valid"] is True


def test_approved_action_gets_token_and_can_pay(client, make_mandate, act):
    m = make_mandate()
    r = act(m["id"], 1200)
    assert r["decision"] == "pending_approval" and r["payment_token"] is None
    apr = r["approval_id"]
    assert client.get(f"/approvals/{apr}").json()["payment_token"] is None
    cb = client.post(f"/approvals/{apr}", json={"decision": "approve"}, headers={"X-Approval-Secret": TEST_SECRET})
    assert cb.status_code == 200
    assert "payment_token" not in cb.json()  # the n8n callback never sees the token
    tok = client.get(f"/approvals/{apr}").json()["payment_token"]
    assert tok
    assert pay(client, tok, amount=1200).json()["status"] == "paid"


def test_denied_action_gets_no_token(client, make_mandate, act):
    m = make_mandate()
    r = act(m["id"], 100, merchant="sketchy-shop.biz")
    assert r["decision"] == "deny" and r["payment_token"] is None


# ---- refusals ---------------------------------------------------------------

def test_forged_token_altered_payload_refused(client, make_mandate, act):
    _, r = allowed_token(make_mandate, act)
    prefix, body, sig = r["payment_token"].split(".")
    claims = json.loads(_b64d(body))
    claims["amount"] = 9000
    forged = f"{prefix}.{_b64e(json.dumps(claims).encode())}.{sig}"
    res = pay(client, forged, amount=9000)
    assert res.status_code == 403
    assert "signature" in res.json()["detail"]["reason"]
    assert "payment.refused" in audit_events(client)


def test_forged_token_signed_with_other_key_refused(client, make_mandate, act):
    _, r = allowed_token(make_mandate, act)
    _, body, _ = r["payment_token"].split(".")
    evil_sig = _b64e(Ed25519PrivateKey.generate().sign(body.encode()))
    res = pay(client, f"mpt1.{body}.{evil_sig}")
    assert res.status_code == 403
    assert "signature" in res.json()["detail"]["reason"]


def test_garbage_token_refused(client):
    res = pay(client, "not-a-token")
    assert res.status_code == 403
    assert "malformed" in res.json()["detail"]["reason"]


def test_reused_token_refused(client, make_mandate, act):
    _, r = allowed_token(make_mandate, act)
    assert pay(client, r["payment_token"]).status_code == 200
    again = pay(client, r["payment_token"])
    assert again.status_code == 409
    assert "already used" in again.json()["detail"]["reason"]
    assert client.get("/wallet").json()["balance"] == 10000 - 300  # debited once


def test_expired_token_refused(client, make_mandate, act, monkeypatch):
    _, r = allowed_token(make_mandate, act)
    monkeypatch.setattr(clock, "now", lambda: datetime.now(timezone.utc) + timedelta(minutes=11))
    res = pay(client, r["payment_token"])
    assert res.status_code == 403
    assert "expired" in res.json()["detail"]["reason"]


def test_wrong_amount_refused(client, make_mandate, act):
    _, r = allowed_token(make_mandate, act)
    res = pay(client, r["payment_token"], amount=301)
    assert res.status_code == 403
    assert "amount mismatch" in res.json()["detail"]["reason"]
    # A refused attempt does not burn the token.
    assert pay(client, r["payment_token"], amount=300).status_code == 200


def test_wrong_merchant_refused(client, make_mandate, act):
    _, r = allowed_token(make_mandate, act)
    res = pay(client, r["payment_token"], merchant="swiggy")  # allowed merchant, but not this token's
    assert res.status_code == 403
    assert "merchant mismatch" in res.json()["detail"]["reason"]


def test_token_from_revoked_mandate_refused(client, make_mandate, act):
    m, r = allowed_token(make_mandate, act)
    client.post(f"/mandates/{m['id']}/revoke")
    res = pay(client, r["payment_token"])
    assert res.status_code == 403
    assert "revoked" in res.json()["detail"]["reason"]
    assert client.get("/wallet").json()["balance"] == 10000


def test_insufficient_balance_refused(client, make_mandate, act, monkeypatch):
    monkeypatch.setenv("MOCK_WALLET_BALANCE", "100")
    _, r = allowed_token(make_mandate, act)
    res = pay(client, r["payment_token"])
    assert res.status_code == 402
    assert "insufficient" in res.json()["detail"]["reason"]


# ---- long-poll + response text -----------------------------------------------

def test_wait_for_approval_times_out_while_pending(client, make_mandate, act):
    m = make_mandate()
    apr = act(m["id"], 1200)["approval_id"]
    res = client.get(f"/approvals/{apr}/wait", params={"timeout": 1}).json()
    assert res["status"] == "pending" and res["timed_out"] is True


def test_wait_for_approval_returns_decision(client, make_mandate, act):
    m = make_mandate()
    apr = act(m["id"], 1200)["approval_id"]
    client.post(f"/approvals/{apr}", json={"decision": "approve"}, headers={"X-Approval-Secret": TEST_SECRET})
    res = client.get(f"/approvals/{apr}/wait", params={"timeout": 30}).json()
    assert res["status"] == "approved" and res["timed_out"] is False and res["payment_token"]


def test_pending_text_without_webhook(make_mandate, act):
    m = make_mandate()
    r = act(m["id"], 1200)
    assert r["approval_id"] in r["reason"]
    assert "phone" not in r["reason"].lower()
    assert "No notification channel is configured" in r["reason"]


def test_pending_text_with_webhook(make_mandate, act, monkeypatch):
    monkeypatch.setenv("N8N_WEBHOOK_URL", "http://127.0.0.1:9/unreachable-test-hook")
    m = make_mandate()
    r = act(m["id"], 1200)
    assert r["approval_id"] in r["reason"]
    assert "owner has been notified" in r["reason"]
