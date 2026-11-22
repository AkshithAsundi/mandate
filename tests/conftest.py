import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_SECRET = "test-approval-secret-not-real"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Fresh DB + throwaway key per test; never touches the real mandate.db / server_key.pem.
    monkeypatch.setenv("MANDATE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MANDATE_KEY_PATH", str(tmp_path / "test_key.pem"))
    monkeypatch.setenv("APPROVAL_SECRET", TEST_SECRET)
    monkeypatch.setenv("N8N_WEBHOOK_URL", "")
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_mandate(client):
    def _make(**overrides):
        body = {
            "agent_id": "test-agent",
            "owner": "tester",
            "currency": "INR",
            "spend_limit_per_week": 5000,
            "approval_threshold": 1000,
            "allowed_merchants": ["amazon.in", "swiggy"],
            "allowed_action_types": ["purchase"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        }
        body.update(overrides)
        r = client.post("/mandates", json=body)
        assert r.status_code == 201, r.text
        return r.json()
    return _make


@pytest.fixture
def act(client):
    def _act(mandate_id, amount, merchant="amazon.in", action_type="purchase"):
        r = client.post("/request_action", json={"mandate_id": mandate_id, "action_type": action_type,
                                                 "merchant": merchant, "amount": amount})
        assert r.status_code == 200, r.text
        return r.json()
    return _act
