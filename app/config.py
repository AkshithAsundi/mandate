"""Runtime configuration. Values are read on every call so tests can override them.

Secrets (APPROVAL_SECRET etc.) come only from the environment / .env and are never logged.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def db_path() -> str:
    return os.getenv("MANDATE_DB", str(ROOT / "mandate.db"))


def key_path() -> str:
    # *.pem is git-ignored.
    return os.getenv("MANDATE_KEY_PATH", str(ROOT / "server_key.pem"))


def n8n_webhook_url() -> str:
    return os.getenv("N8N_WEBHOOK_URL", "").strip()


def approval_secret() -> str:
    return os.getenv("APPROVAL_SECRET", "").strip()


def mock_wallet_start_balance() -> float:
    return float(os.getenv("MOCK_WALLET_BALANCE", "50000"))


def base_url() -> str:
    return os.getenv("MANDATE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
