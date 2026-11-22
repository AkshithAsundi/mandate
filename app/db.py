"""SQLite storage. One short-lived connection per operation; schema created on first use."""
import sqlite3
import threading

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mandates (
    id          TEXT PRIMARY KEY,
    body        TEXT NOT NULL,      -- canonical JSON of the signed fields
    signature   TEXT NOT NULL,      -- base64 Ed25519 signature over body
    revoked     INTEGER NOT NULL DEFAULT 0,
    revoked_at  TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id           TEXT PRIMARY KEY,
    mandate_id   TEXT NOT NULL,
    action_type  TEXT NOT NULL,
    merchant     TEXT NOT NULL,
    amount       REAL NOT NULL,
    decision     TEXT NOT NULL,     -- allow | deny | pending_approval
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    action_id   TEXT NOT NULL,
    mandate_id  TEXT NOT NULL,
    status      TEXT NOT NULL,      -- pending | approved | denied
    reason      TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);
CREATE TABLE IF NOT EXISTS payment_tokens (
    jti         TEXT PRIMARY KEY,
    token       TEXT NOT NULL,
    mandate_id  TEXT NOT NULL,
    action_id   TEXT NOT NULL,
    merchant    TEXT NOT NULL,
    amount      REAL NOT NULL,
    currency    TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,               -- set once, atomically, when paid
    payment_id  TEXT
);
CREATE TABLE IF NOT EXISTS wallet (
    id        TEXT PRIMARY KEY,     -- single mock wallet: 'default'
    currency  TEXT NOT NULL,
    balance   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id          TEXT PRIMARY KEY,
    jti         TEXT NOT NULL UNIQUE,
    mandate_id  TEXT NOT NULL,
    action_id   TEXT NOT NULL,
    merchant    TEXT NOT NULL,
    amount      REAL NOT NULL,
    currency    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    event      TEXT NOT NULL,
    payload    TEXT NOT NULL,       -- canonical JSON
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
"""

_initialized: set[str] = set()
_init_lock = threading.Lock()
# Serialises multi-statement writes (audit chain append, weekly-spend check + insert).
write_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    path = config.db_path()
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if path not in _initialized:
        with _init_lock:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized.add(path)
    return conn
