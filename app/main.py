"""Mandate policy server.

Run:  .venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Docs: http://127.0.0.1:8000/docs
"""
import asyncio
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from . import approvals, audit, clock, config, crypto, db, tokens, wallet
from .models import (ActionRequest, ActionResponse, Approval, ApprovalDecision, Mandate,
                     MandateCreate)
from .policy import evaluate, parse_ts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mandate")

app = FastAPI(title="Mandate", version="0.2.0",
              description="Signed, revocable mandates for AI agents. Payments are simulated (mock wallet).")
app.include_router(wallet.router)

MAX_WAIT_SECONDS = 300


def _now() -> datetime:
    return clock.now()


_iso = clock.iso


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


def _approval_out(conn, approval_id: str, include_token: bool = True) -> Approval | None:
    row = conn.execute(
        """SELECT p.*, a.action_type, a.merchant, a.amount
           FROM approvals p JOIN actions a ON a.id = p.action_id WHERE p.id = ?""",
        (approval_id,),
    ).fetchone()
    if not row:
        return None
    out = Approval(approval_id=row["id"], action_id=row["action_id"], mandate_id=row["mandate_id"],
                   status=row["status"], reason=row["reason"], action_type=row["action_type"],
                   merchant=row["merchant"], amount=row["amount"], created_at=row["created_at"],
                   decided_at=row["decided_at"])
    if include_token and out.status == "approved":
        tok = tokens.for_action(conn, out.action_id)
        if tok:
            out.payment_token = tok["token"]
            out.payment_token_expires_at = tok["expires_at"]
            out.payment_token_used = tok["used_at"] is not None
    return out


def _pending_reason(policy_reason: str, approval_id: str) -> str:
    if config.n8n_webhook_url():
        how = "The owner has been notified and must approve or deny it."
    else:
        how = ("No notification channel is configured, so the owner must approve or deny it "
               "directly on the Mandate server.")
    return (f"{policy_reason}. Approval request {approval_id} created. {how} "
            f"Wait for the decision on approval_id {approval_id}; do not proceed before it is approved.")


# ----------------------------------------------------------------- routes --

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/public_key")
def public_key():
    """Ed25519 public key (base64, raw 32 bytes) for verifying mandate signatures and payment tokens."""
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
                # Unused payment tokens become worthless: the wallet re-checks revocation at pay time.
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
    token = None
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
            reason = d.reason
            if d.decision == "pending_approval":
                approval_id = "apr_" + uuid.uuid4().hex[:16]
                reason = _pending_reason(d.reason, approval_id)
            conn.execute(
                "INSERT INTO actions (id, mandate_id, action_type, merchant, amount, decision, reason, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (action_id, req.mandate_id, req.action_type, req.merchant, req.amount, d.decision, reason, _iso(now)))
            audit.append("action.requested", {"action_id": action_id, **req.model_dump(),
                                              "decision": d.decision, "reason": reason}, conn)
            if d.decision == "allow":
                token = tokens.issue(conn, mandate_id=req.mandate_id, action_id=action_id,
                                     merchant=req.merchant, amount=req.amount, currency=mandate["currency"])
                audit.append("token.issued", {"jti": token["jti"], "action_id": action_id}, conn)
            elif approval_id:
                conn.execute(
                    "INSERT INTO approvals (id, action_id, mandate_id, status, created_at) VALUES (?,?,?,?,?)",
                    (approval_id, action_id, req.mandate_id, "pending", _iso(now)))
                audit.append("approval.created", {"approval_id": approval_id, "action_id": action_id}, conn)
            conn.commit()
            approval = _approval_out(conn, approval_id).model_dump() if approval_id else None
    finally:
        conn.close()

    if approval:
        approval.update(agent_id=mandate["agent_id"], owner=mandate["owner"], currency=mandate["currency"],
                        policy_reason=d.reason)
        for k in ("payment_token", "payment_token_expires_at", "payment_token_used", "timed_out"):
            approval.pop(k, None)
        background.add_task(approvals.notify, approval)

    return ActionResponse(action_id=action_id, decision=d.decision, reason=reason, approval_id=approval_id,
                          payment_token=token["token"] if token else None,
                          payment_token_expires_at=token["expires_at"] if token else None)


@app.post("/approvals/{approval_id}", response_model=Approval, response_model_exclude_none=True)
def decide_approval(approval_id: str, body: ApprovalDecision,
                    x_approval_secret: str | None = Header(default=None)):
    """Callback for n8n / the owner. Requires header X-Approval-Secret == APPROVAL_SECRET.
    The response never contains the payment token - only the agent's poll does."""
    expected = config.approval_secret()
    if not expected:
        raise HTTPException(503, "APPROVAL_SECRET is not configured on the server")
    if not x_approval_secret or not hmac.compare_digest(x_approval_secret.encode(), expected.encode()):
        audit.append("approval.rejected_callback", {"approval_id": approval_id, "reason": "bad or missing secret"})
        raise HTTPException(401, "missing or invalid X-Approval-Secret")

    conn = db.connect()
    try:
        with db.write_lock:
            current = _approval_out(conn, approval_id, include_token=False)
            if not current:
                raise HTTPException(404, "approval not found")
            if current.status != "pending":
                raise HTTPException(409, f"approval already {current.status}")
            now = _now()
            status, reason = ("approved", "approved by owner") if body.decision == "approve" \
                else ("denied", "denied by owner")
            mandate = _load_mandate(conn, current.mandate_id)
            if status == "approved":
                # Re-check the mandate: it may have expired or been revoked while waiting.
                if mandate["revoked"]:
                    status, reason = "denied", "mandate revoked before approval"
                elif now >= parse_ts(mandate["expires_at"]):
                    status, reason = "denied", "mandate expired before approval"
            conn.execute("UPDATE approvals SET status=?, reason=?, decided_at=? WHERE id=?",
                         (status, reason, _iso(now), approval_id))
            audit.append("approval.decided", {"approval_id": approval_id, "decision": body.decision,
                                              "status": status, "reason": reason}, conn)
            if status == "approved":
                tok = tokens.issue(conn, mandate_id=current.mandate_id, action_id=current.action_id,
                                   merchant=current.merchant, amount=current.amount, currency=mandate["currency"])
                audit.append("token.issued", {"jti": tok["jti"], "action_id": current.action_id}, conn)
            conn.commit()
            result = _approval_out(conn, approval_id, include_token=False)
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


@app.get("/approvals/{approval_id}/wait", response_model=Approval)
async def wait_for_approval(approval_id: str, timeout: float = 120):
    """Long-poll: returns as soon as the approval is decided, or after `timeout` seconds
    (max 300) with status 'pending' and timed_out=true."""
    timeout = min(max(timeout, 0.0), MAX_WAIT_SECONDS)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = get_approval(approval_id)  # raises 404 if unknown
        if result.status != "pending":
            return result
        if loop.time() >= deadline:
            result.timed_out = True
            return result
        await asyncio.sleep(0.5)


@app.get("/audit/verify")
def audit_verify():
    return audit.verify()


@app.get("/audit")
def audit_entries(limit: int = 100):
    return audit.entries(min(max(limit, 1), 1000))
