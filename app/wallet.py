"""Mock wallet. SIMULATED payments only - no real money moves.

The wallet's only entry point for spending is pay(token, merchant, amount). It accepts nothing
but a one-time authorization token issued by the policy server, and checks, in order:
  1. token is well-formed and its Ed25519 signature verifies (not forged / not altered)
  2. token was issued by this server (known jti) and matches the stored record
  3. token has not expired (10 min TTL)
  4. merchant and amount are exactly what the token authorizes
  5. token has not been used before (single use, enforced atomically)
  6. the mandate is still valid: signature intact, not revoked, not expired
  7. the mock balance covers the amount
Every refusal is audited with its reason (never the token itself, only its jti).
"""
import json
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import audit, clock, config, crypto, db, tokens
from .policy import parse_ts

router = APIRouter(prefix="/wallet", tags=["mock wallet"])


class PayRequest(BaseModel):
    token: str = Field(description="One-time payment authorization token from Mandate")
    merchant: str
    amount: float = Field(gt=0)


class PaymentRefused(Exception):
    def __init__(self, reason: str, status: int = 403):
        super().__init__(reason)
        self.reason, self.status = reason, status


def _norm(s: str) -> str:
    return s.strip().casefold()


def _ensure_wallet(conn) -> dict:
    conn.execute("INSERT OR IGNORE INTO wallet (id, currency, balance) VALUES ('default', 'INR', ?)",
                 (config.mock_wallet_start_balance(),))
    return dict(conn.execute("SELECT * FROM wallet WHERE id='default'").fetchone())


def pay(token: str, merchant: str, amount: float) -> dict:
    conn = db.connect()
    jti = None
    try:
        with db.write_lock:
            try:
                # 1. signature
                try:
                    claims = tokens.verify(token)
                except tokens.TokenError as e:
                    raise PaymentRefused(str(e))
                jti = claims.get("jti")
                # 2. issued by us, record matches claims
                rec = conn.execute("SELECT * FROM payment_tokens WHERE jti=?", (jti,)).fetchone()
                if not rec or rec["token"] != token:
                    raise PaymentRefused("token was not issued by this server")
                # 3. expiry
                now = clock.now()
                if now >= parse_ts(claims["exp"]):
                    raise PaymentRefused(f"token expired at {claims['exp']}")
                # 4. exact merchant / amount
                if _norm(merchant) != _norm(claims["merchant"]):
                    raise PaymentRefused(
                        f"merchant mismatch: token authorizes '{claims['merchant']}', not '{merchant}'")
                if round(float(amount), 2) != round(float(claims["amount"]), 2):
                    raise PaymentRefused(
                        f"amount mismatch: token authorizes {claims['amount']:g}, not {amount:g}")
                # 5. single use
                if rec["used_at"]:
                    raise PaymentRefused(f"token already used (payment {rec['payment_id']})", 409)
                # 6. mandate still valid
                m = conn.execute("SELECT * FROM mandates WHERE id=?", (claims["mandate_id"],)).fetchone()
                if not m:
                    raise PaymentRefused("mandate not found")
                if not crypto.verify(m["body"], m["signature"]):
                    raise PaymentRefused("mandate signature is invalid (mandate was altered)")
                if m["revoked"]:
                    raise PaymentRefused("mandate has been revoked")
                if now >= parse_ts(json.loads(m["body"])["expires_at"]):
                    raise PaymentRefused("mandate has expired")
                # 7. balance
                wallet = _ensure_wallet(conn)
                if wallet["currency"] != claims["currency"]:
                    raise PaymentRefused(f"currency mismatch: wallet is {wallet['currency']}")
                if wallet["balance"] < claims["amount"]:
                    raise PaymentRefused(f"insufficient mock balance ({wallet['balance']:g} {wallet['currency']})", 402)

                payment_id = "pay_" + uuid.uuid4().hex[:16]
                ts = clock.iso(now)
                cur = conn.execute("UPDATE payment_tokens SET used_at=?, payment_id=? WHERE jti=? AND used_at IS NULL",
                                   (ts, payment_id, jti))
                if cur.rowcount != 1:  # lost a race with another pay() of the same token
                    raise PaymentRefused("token already used", 409)
                conn.execute("UPDATE wallet SET balance = balance - ? WHERE id='default'", (claims["amount"],))
                conn.execute("INSERT INTO payments (id, jti, mandate_id, action_id, merchant, amount, currency, created_at) "
                             "VALUES (?,?,?,?,?,?,?,?)",
                             (payment_id, jti, claims["mandate_id"], claims["action_id"], claims["merchant"],
                              claims["amount"], claims["currency"], ts))
                balance = conn.execute("SELECT balance FROM wallet WHERE id='default'").fetchone()["balance"]
                result = {"status": "paid", "simulated": True, "payment_id": payment_id,
                          "mandate_id": claims["mandate_id"], "action_id": claims["action_id"],
                          "merchant": claims["merchant"], "amount": claims["amount"],
                          "currency": claims["currency"], "balance_after": balance}
                audit.append("payment.completed", {k: v for k, v in result.items() if k != "status"} | {"jti": jti}, conn)
                conn.commit()
                return result
            except PaymentRefused as e:
                conn.rollback()
                audit.append("payment.refused", {"jti": jti, "merchant": merchant, "amount": amount,
                                                 "reason": e.reason}, conn)
                conn.commit()
                raise
    finally:
        conn.close()


@router.post("/pay")
def pay_endpoint(req: PayRequest):
    """Pay a merchant (SIMULATED). Requires a one-time token from /request_action or an approval."""
    try:
        return pay(req.token, req.merchant, req.amount)
    except PaymentRefused as e:
        raise HTTPException(e.status, {"status": "refused", "reason": e.reason})


@router.get("")
def wallet_status(limit: int = 20):
    conn = db.connect()
    try:
        with db.write_lock:
            w = _ensure_wallet(conn)
            conn.commit()
        rows = conn.execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT ?",
                            (min(max(limit, 1), 200),)).fetchall()
    finally:
        conn.close()
    return {"simulated": True, "currency": w["currency"], "balance": w["balance"],
            "recent_payments": [dict(r) for r in rows]}
