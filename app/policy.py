"""Policy engine: pure function, no I/O, so every rule is easy to unit-test.

Rules run in this order; the first one that fires decides:
  1. mandate signature invalid   -> deny
  2. revoked                      -> deny
  3. expired                      -> deny
  4. action type not allowed      -> deny
  5. merchant not allowed         -> deny
  6. weekly spend limit exceeded  -> deny
  7. amount > approval_threshold  -> pending_approval
  8. otherwise                    -> allow
"""
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Decision:
    decision: str  # allow | deny | pending_approval
    reason: str


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm(s: str) -> str:
    return s.strip().lower()


def evaluate(mandate: dict, action_type: str, merchant: str, amount: float,
             spent_this_week: float, now: datetime, signature_valid: bool = True) -> Decision:
    cur = mandate.get("currency", "")
    if not signature_valid:
        return Decision("deny", "mandate signature is invalid (mandate was altered)")
    if mandate["revoked"]:
        return Decision("deny", "mandate has been revoked")
    if now >= parse_ts(mandate["expires_at"]):
        return Decision("deny", f"mandate expired at {mandate['expires_at']}")
    if _norm(action_type) not in {_norm(a) for a in mandate["allowed_action_types"]}:
        return Decision("deny", f"action type '{action_type}' is not allowed by this mandate")
    if _norm(merchant) not in {_norm(m) for m in mandate["allowed_merchants"]}:
        return Decision("deny", f"merchant '{merchant}' is not on the allowed list")
    limit = mandate["spend_limit_per_week"]
    if spent_this_week + amount > limit:
        return Decision(
            "deny",
            f"weekly limit exceeded: {spent_this_week:g} already committed + {amount:g} "
            f"> {limit:g} {cur}".rstrip(),
        )
    threshold = mandate["approval_threshold"]
    if amount > threshold:
        return Decision("pending_approval",
                        f"amount {amount:g} is above the approval threshold {threshold:g}; owner approval needed")
    return Decision("allow", f"within mandate (amount {amount:g} <= threshold {threshold:g})")
