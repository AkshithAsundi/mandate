import sqlite3
from datetime import datetime, timedelta, timezone


from app import clock, config, crypto
from tests.conftest import TEST_SECRET


# ---- policy rules -----------------------------------------------------------

def test_under_threshold_is_allowed(make_mandate, act):
    m = make_mandate()
    r = act(m["id"], 300)
    assert r["decision"] == "allow"
    assert r["approval_id"] is None


def test_over_threshold_is_pending(make_mandate, act):
    m = make_mandate()
    r = act(m["id"], 1200)
    assert r["decision"] == "pending_approval"
    assert r["approval_id"].startswith("apr_")


def test_unlisted_merchant_is_denied(make_mandate, act):
    m = make_mandate()
    r = act(m["id"], 100, merchant="sketchy-shop.biz")
    assert r["decision"] == "deny"
    assert "not on the allowed list" in r["reason"]


def test_disallowed_action_type_is_denied(make_mandate, act):
    m = make_mandate()
    r = act(m["id"], 100, action_type="transfer")
    assert r["decision"] == "deny"
    assert "action type" in r["reason"]


def test_over_weekly_limit_is_denied(make_mandate, act):
    m = make_mandate(spend_limit_per_week=2000, approval_threshold=1000)
    assert act(m["id"], 900)["decision"] == "allow"
    assert act(m["id"], 900)["decision"] == "allow"
    r = act(m["id"], 300)  # 1800 + 300 > 2000
    assert r["decision"] == "deny"
    assert "weekly limit" in r["reason"]


def test_pending_approval_reserves_weekly_budget(make_mandate, act):
    m = make_mandate(spend_limit_per_week=2000, approval_threshold=1000)
    assert act(m["id"], 1500)["decision"] == "pending_approval"
    assert act(m["id"], 600)["decision"] == "deny"  # 1500 reserved + 600 > 2000


def test_expired_mandate_is_denied(make_mandate, act, monkeypatch):
    m = make_mandate(expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(clock, "now", lambda: datetime.now(timezone.utc) + timedelta(hours=1))
    r = act(m["id"], 100)
    assert r["decision"] == "deny"
    assert "expired" in r["reason"]


def test_revoked_mandate_is_denied(client, make_mandate, act):
    m = make_mandate()
    assert act(m["id"], 100)["decision"] == "allow"
    assert client.post(f"/mandates/{m['id']}/revoke").json()["revoked"] is True
    r = act(m["id"], 100)
    assert r["decision"] == "deny"
    assert "revoked" in r["reason"]


def test_tampered_mandate_signature_is_denied(make_mandate, act):
    m = make_mandate()
    conn = sqlite3.connect(config.db_path())
    body = conn.execute("SELECT body FROM mandates WHERE id=?", (m["id"],)).fetchone()[0]
    conn.execute("UPDATE mandates SET body=? WHERE id=?",
                 (body.replace('"spend_limit_per_week":5000', '"spend_limit_per_week":999999'), m["id"]))
    conn.commit(); conn.close()
    r = act(m["id"], 100)
    assert r["decision"] == "deny"
    assert "signature" in r["reason"]


def test_mandate_signature_verifies_with_public_key(client, make_mandate):
    m = make_mandate()
    assert m["signature_valid"] is True
    assert m["public_key"] == client.get("/public_key").json()["public_key"]
    assert crypto.verify(m["signed_body"], m["signature"])


# ---- approvals --------------------------------------------------------------

def test_approval_callback_rejected_without_secret(client, make_mandate, act):
    m = make_mandate()
    apr = act(m["id"], 1200)["approval_id"]
    assert client.post(f"/approvals/{apr}", json={"decision": "approve"}).status_code == 401
    bad = client.post(f"/approvals/{apr}", json={"decision": "approve"},
                      headers={"X-Approval-Secret": "wrong"})
    assert bad.status_code == 401
    assert client.get(f"/approvals/{apr}").json()["status"] == "pending"


def test_full_approval_flow(client, make_mandate, act):
    m = make_mandate()
    apr = act(m["id"], 1200)["approval_id"]
    assert client.get(f"/approvals/{apr}").json()["status"] == "pending"
    r = client.post(f"/approvals/{apr}", json={"decision": "approve"},
                    headers={"X-Approval-Secret": TEST_SECRET})
    assert r.status_code == 200
    assert client.get(f"/approvals/{apr}").json()["status"] == "approved"
    # A second decision is refused.
    again = client.post(f"/approvals/{apr}", json={"decision": "deny"},
                        headers={"X-Approval-Secret": TEST_SECRET})
    assert again.status_code == 409
    assert client.get("/audit/verify").json()["valid"] is True


def test_revoke_cancels_pending_approval(client, make_mandate, act):
    m = make_mandate()
    apr = act(m["id"], 1200)["approval_id"]
    client.post(f"/mandates/{m['id']}/revoke")
    a = client.get(f"/approvals/{apr}").json()
    assert a["status"] == "denied"
    assert "revoked" in a["reason"]


# ---- audit log --------------------------------------------------------------

def test_audit_chain_detects_tampering(client, make_mandate, act):
    m = make_mandate()
    act(m["id"], 300)
    act(m["id"], 1200)
    v = client.get("/audit/verify").json()
    assert v["valid"] is True and v["entries"] >= 4

    conn = sqlite3.connect(config.db_path())
    conn.execute("UPDATE audit_log SET payload = replace(payload, '300', '3') WHERE seq = 2")
    conn.commit(); conn.close()

    v = client.get("/audit/verify").json()
    assert v["valid"] is False
    assert v["broken_at_seq"] == 2


def test_audit_chain_detects_deleted_entry(client, make_mandate, act):
    m = make_mandate()
    act(m["id"], 300)
    act(m["id"], 400)
    conn = sqlite3.connect(config.db_path())
    conn.execute("DELETE FROM audit_log WHERE seq = 2")
    conn.commit(); conn.close()
    assert client.get("/audit/verify").json()["valid"] is False
