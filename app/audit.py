"""Hash-chained audit log.

Each row stores prev_hash (the previous row's hash) and
hash = sha256(prev_hash | seq | ts | event | payload). Editing, deleting or reordering any
row breaks the chain from that point on, which verify() reports.
"""
import hashlib
from datetime import datetime, timezone

from . import db
from .crypto import canonical_json

GENESIS = "0" * 64


def _row_hash(prev_hash: str, seq: int, ts: str, event: str, payload: str) -> str:
    material = "|".join([prev_hash, str(seq), ts, event, payload])
    return hashlib.sha256(material.encode()).hexdigest()


def append(event: str, payload: dict, conn=None) -> str:
    own = conn is None
    conn = conn or db.connect()
    try:
        with db.write_lock:
            last = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = last["hash"] if last else GENESIS
            seq = (last["seq"] + 1) if last else 1
            ts = datetime.now(timezone.utc).isoformat()
            body = canonical_json(payload)
            h = _row_hash(prev_hash, seq, ts, event, body)
            conn.execute(
                "INSERT INTO audit_log (seq, ts, event, payload, prev_hash, hash) VALUES (?,?,?,?,?,?)",
                (seq, ts, event, body, prev_hash, h),
            )
            if own:
                conn.commit()
        return h
    finally:
        if own:
            conn.close()


def verify() -> dict:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
    finally:
        conn.close()
    prev = GENESIS
    expected_seq = 1
    for r in rows:
        if r["seq"] != expected_seq:
            return {"valid": False, "entries": len(rows), "broken_at_seq": r["seq"],
                    "reason": f"missing entry: expected seq {expected_seq}"}
        if r["prev_hash"] != prev:
            return {"valid": False, "entries": len(rows), "broken_at_seq": r["seq"],
                    "reason": "prev_hash does not match previous entry"}
        if _row_hash(r["prev_hash"], r["seq"], r["ts"], r["event"], r["payload"]) != r["hash"]:
            return {"valid": False, "entries": len(rows), "broken_at_seq": r["seq"],
                    "reason": "entry contents do not match its hash"}
        prev = r["hash"]
        expected_seq += 1
    return {"valid": True, "entries": len(rows), "head_hash": prev}


def entries(limit: int = 100) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
