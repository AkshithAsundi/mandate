"""Mandate policy server (M1).

Run:  .venv/Scripts/python -m uvicorn app.main:app --reload
Docs: http://127.0.0.1:8000/docs
"""
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from . import approvals, audit, config, crypto, db
from .models import (ActionRequest, ActionResponse, Approval, ApprovalDecision, Mandate,
                     MandateCreate)
from .policy import evaluate, parse_ts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mandate")

app = FastAPI(title="Mandate", version="0.1.0",
              description="Signed, revocable mandates for AI agents. Payments are simulated.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------- helpers --

def _load_mandate(conn, mandate_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM mandates WHERE id = ?", (mandate_id,)).fetchone()
    if not row:
        return None
    body = json.loads(row["body"])
    return {
        **body,
        "revoked": bool(row["revoked"]),
        "revoked_at": row["revoked_at"],
        "signature": row["signature"],
        "signed_body": row["body"],
        "signature_valid": crypto.verify(row["body"], row["signature"]),
    }


def _committed_this_week(conn, mandate_id: str, now: datetime) -> float:
    """Allowed actions + approvals that are approved or still pending (reserved), last 7 days."""
    since = _iso(now - timedelta(days=7))
    row = conn.execute(
        """
        SELECT COALESCE(SUM(a.amount), 0) AS total
        FROM actions a LEFT JOIN approvals p ON p.action_id = a.id
        WHERE a.mandate_id = ? AND a.created_at >= ?
          AND (a.decision = 'allow'
               OR (a.decision = 'pending_approval' AND p.status IN ('pending', 'approved')))
        """,
        (mandate_id, since),
    ).fetchone()
    return float(row["total"])


def _approval_out(conn, approval_id: str) -> Approval | None:
    row = conn.execute(
        """SELECT p.*, a.action_type, a.merchant, a.amount
           FROM approvals p JOIN actions a ON a.id = p.action_id WHERE p.id = ?""",
        (approval_id,),
    ).fetchone()
    if not row:
        return None
    return Approval(approval_id=row["id"], action_id=row["action_id"], mandate_id=row["mandate_id"],
                    status=row["status"], reason=row["reason"], action_type=row["action_type"],
                    merchant=row["merchant"], amount=row["amount"], created_at=row["created_at"],
                    decided_at=row["decided_at"])


# ----------------------------------------------------------------- routes --

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/public_key")
def public_key():
    """Ed25519 public key (base64, raw 32 bytes) for verifying mandate signatures."""
    return {"alg": "Ed25519", "public_key": crypto.public_key_b64()}


@app.post("/mandates", response_model=Mandate, status_code=201)
def create_mandate(m: MandateCreate):
    now = _now()
    if _iso(m.expires_at) <= _iso(now):
        raise HTTPException(400, "expires_at must be in the future")
    mandate_id = "mdt_" + uuid.uuid4().hex[:16]
    body = {
        "id": mandate_id,
        "agent_id": m.agent_id,
        "owner": m.owner,
        "currency": m.currency.upper(),
        "spend_limit_per_week": m.spend_limit_per_week,
        "approval_threshold": m.approval_threshold,
        "allowed_merchants": m.allowed_merchants,
        "allowed_action_types": m.allowed_action_types,
        "expires_at": _iso(m.expires_at),
        "issued_at": _iso(now),
    }
    signed_body = crypto.canonical_json(body)
    signature = crypto.sign(signed_body)
    conn = db.connect()
    try:
        with db.write_lock:
            conn.execute("INSERT INTO mandates (id, body, signature, created_at) VALUES (?,?,?,?)",
                         (mandate_id, signed_body, signature, _iso(now)))
            audit.append("mandate.created", {"mandate": body, "signature": signature}, conn)
            conn.commit()
        mandate = _load_mandate(conn, mandate_id)
    finally:
        conn.close()
    return Mandate(**mandate, public_key=crypto.public_key_b64())


@app.get("/mandates/{mandate_id}", response_model=Mandate)
def get_mandate(mandate_id: str):
    conn = db.connect()
    try:
        mandate = _load_mandate(conn, mandate_id)
    finally:
        conn.close()
    if not mandate:
        raise HTTPException(404, "mandate not found")
    return Mandate(**mandate, public_key=crypto.public_key_b64())


@app.post("/mandates/{mandate_id}/revoke", response_model=Mandate)
def revoke_mandate(mandate_id: str):
    conn = db.connect()
    try:
        with db.write_lock:
            mandate = _load_mandate(conn, mandate_id)
            if not mandate:
                raise HTTPException(404, "mandate not found")
            if not mandate["revoked"]:
                now = _iso(_now())
                conn.execute("UPDATE mandates SET revoked = 1, revoked_at = ? WHERE id = ?", (now, mandate_id))
                # Any approval still waiting on the owner dies with the mandate.
                conn.execute(
                    "UPDATE approvals SET status='denied', reason='mandate revoked', decided_at=? "
                    "WHERE mandate_id=? AND status='pending'", (now, mandate_id))
                audit.append("mandate.revoked", {"mandate_id": mandate_id}, conn)
                conn.commit()
            mandate = _load_mandate(conn, mandate_id)
    finally:
        conn.close()
    return Mandate(**mandate, public_key=crypto.public_key_b64())


@app.post("/request_action", response_model=ActionResponse)
def request_action(req: ActionRequest, background: BackgroundTasks):
    now = _now()
    action_id = "act_" + uuid.uuid4().hex[:16]
    approval_id = None
    conn = db.connect()
    try:
        with db.write_lock:  # spend check + insert must be atomic
            mandate = _load_mandate(conn, req.mandate_id)
            if not mandate:
                audit.append("action.requested", {"action_id": action_id, **req.model_dump(),
                                                  "decision": "deny", "reason": "mandate not found"}, conn)
                conn.commit()
                raise HTTPException(404, "mandate not found")
            spent = _committed_this_week(conn, req.mandate_id, now)
            d = evaluate(mandate, req.action_type, req.merchant, req.amount, spent, now,
                         signature_valid=mandate["signature_valid"])
            conn.execute(
                "INSERT INTO actions (id, mandate_id, action_type, merchant, amount, decision, reason, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (action_id, req.mandate_id, req.action_type, req.merchant, req.amount, d.decision, d.reason, _iso(now)))
            audit.append("action.requested", {"action_id": action_id, **req.model_dump(),
                                              "decision": d.decision, "reason": d.reason}, conn)
            if d.decision == "pending_approval":
                approval_id = "apr_" + uuid.uuid4().hex[:16]
                conn.execute(
                    "INSERT INTO approvals (id, action_id, mandate_id, status, created_at) VALUES (?,?,?,?,?)",
                    (approval_id, action_id, req.mandate_id, "pending", _iso(now)))
                audit.append("approval.created", {"approval_id": approval_id, "action_id": action_id}, conn)
            conn.commit()
            approval = _approval_out(conn, approval_id).model_dump() if approval_id else None
    finally:
        conn.close()

    if approval:
        approval["agent_id"] = mandate["agent_id"]
        approval["owner"] = mandate["owner"]
        approval["currency"] = mandate["currency"]
        background.add_task(approvals.notify, approval)

    return ActionResponse(action_id=action_id, decision=d.decision, reason=d.reason, approval_id=approval_id)


@app.post("/approvals/{approval_id}", response_model=Approval)
def decide_approval(approval_id: str, body: ApprovalDecision,
                    x_approval_secret: str | None = Header(default=None)):
    """Callback for n8n. Requires header X-Approval-Secret == APPROVAL_SECRET."""
    expected = config.approval_secret()
    if not expected:
        raise HTTPException(503, "APPROVAL_SECRET is not configured on the server")
    if not x_approval_secret or not hmac.compare_digest(x_approval_secret.encode(), expected.encode()):
        audit.append("approval.rejected_callback", {"approval_id": approval_id, "reason": "bad or missing secret"})
        raise HTTPException(401, "missing or invalid X-Approval-Secret")

    conn = db.connect()
    try:
        with db.write_lock:
            current = _approval_out(conn, approval_id)
            if not current:
                raise HTTPException(404, "approval not found")
            if current.status != "pending":
                raise HTTPException(409, f"approval already {current.status}")
            now = _now()
            status, reason = ("approved", "approved by owner") if body.decision == "approve" \
                else ("denied", "denied by owner")
            if status == "approved":
                # Re-check the mandate: it may have expired or been revoked while waiting.
                mandate = _load_mandate(conn, current.mandate_id)
                if mandate["revoked"]:
                    status, reason = "denied", "mandate revoked before approval"
                elif now >= parse_ts(mandate["expires_at"]):
                    status, reason = "denied", "mandate expired before approval"
            conn.execute("UPDATE approvals SET status=?, reason=?, decided_at=? WHERE id=?",
                         (status, reason, _iso(now), approval_id))
            audit.append("approval.decided", {"approval_id": approval_id, "decision": body.decision,
                                              "status": status, "reason": reason}, conn)
            conn.commit()
            result = _approval_out(conn, approval_id)
    finally:
        conn.close()
    return result


@app.get("/approvals/{approval_id}", response_model=Approval)
def get_approval(approval_id: str):
    conn = db.connect()
    try:
        result = _approval_out(conn, approval_id)
    finally:
        conn.close()
    if not result:
        raise HTTPException(404, "approval not found")
    return result


@app.get("/audit/verify")
def audit_verify():
    return audit.verify()


@app.get("/audit")
def audit_entries(limit: int = 100):
    return audit.entries(min(max(limit, 1), 1000))
