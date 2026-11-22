"""Mandate MCP server (stdio) for Claude Code / Claude Desktop.

Tools: request_action, wait_for_approval, pay, get_mandate, check_approval.
Each tool calls the Mandate API at MANDATE_BASE_URL (default http://127.0.0.1:8000), which must be running.

Run (normally launched by the MCP client, not by hand):
    .venv/Scripts/python mcp_server/server.py

stdout is the MCP protocol channel - never print() here; logs go to stderr.
This process reads ONLY MANDATE_BASE_URL from .env; it never loads APPROVAL_SECRET or other keys.
Uses the official MCP Python SDK 2.x, where FastMCP is named MCPServer.
"""
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values
from mcp.server.mcpserver import MCPServer

logging.basicConfig(stream=sys.stderr, level=logging.INFO)

_env_file = Path(__file__).resolve().parent.parent / ".env"
BASE_URL = (os.getenv("MANDATE_BASE_URL")
            or dotenv_values(_env_file).get("MANDATE_BASE_URL")
            or "http://127.0.0.1:8000").rstrip("/")
MAX_WAIT = 300

mcp = MCPServer(
    name="mandate",
    instructions=(
        "Mandate is the owner's permission layer and the ONLY way to pay. Payments are simulated.\n"
        "Workflow for any purchase or other action that spends the owner's money:\n"
        "1. Call request_action(mandate_id, action_type, merchant, amount).\n"
        "2. decision 'allow' -> the response contains payment_token. Call pay(payment_token, merchant, amount) "
        "with exactly the same merchant and amount.\n"
        "3. decision 'deny' -> stop. Tell the user the reason. Do not retry with altered details to get around it.\n"
        "4. decision 'pending_approval' -> tell the user an approval request was created (quote the reason and "
        "approval_id), then call wait_for_approval(approval_id). It blocks until the owner decides, so do not "
        "poll check_approval in a loop. If it returns status 'approved', it includes payment_token: call pay(). "
        "If 'denied', stop. If timed_out, tell the user it is still pending and call wait_for_approval again "
        "if they want to keep waiting.\n"
        "Payment tokens are single-use, bound to one merchant and amount, and expire after 10 minutes."
    ),
)


def _call(method: str, path: str, timeout: float = 10.0, **kwargs) -> dict:
    try:
        r = httpx.request(method, f"{BASE_URL}{path}", timeout=timeout, **kwargs)
    except httpx.HTTPError as e:
        return {"error": f"cannot reach Mandate server at {BASE_URL} ({type(e).__name__}). "
                         "Is it running? Start it with: .venv/Scripts/python -m uvicorn app.main:app"}
    try:
        data = r.json()
    except ValueError:
        data = {"detail": r.text}
    if r.status_code >= 400:
        detail = data.get("detail", data) if isinstance(data, dict) else data
        if isinstance(detail, dict):
            return {**detail, "status_code": r.status_code}
        return {"error": detail, "status_code": r.status_code}
    return data


@mcp.tool()
def request_action(mandate_id: str, action_type: str, merchant: str, amount: float) -> dict:
    """Ask Mandate for permission BEFORE any purchase or action that spends the owner's money.

    Args:
        mandate_id: The mandate you act under (e.g. "mdt_...").
        action_type: Kind of action, e.g. "purchase".
        merchant: Merchant / counterparty name exactly as you will pay it, e.g. "FreshMart".
        amount: Amount in the mandate's currency (e.g. INR).

    Returns {decision, reason, action_id, approval_id?, payment_token?}:
      - "allow": includes a one-time payment_token -> next call pay(payment_token, merchant, amount).
      - "pending_approval": includes approval_id -> next call wait_for_approval(approval_id).
      - "deny": do not proceed; report the reason to the user.
    """
    return _call("POST", "/request_action", json={
        "mandate_id": mandate_id, "action_type": action_type, "merchant": merchant, "amount": amount})


@mcp.tool()
def wait_for_approval(approval_id: str, timeout_seconds: int = 120) -> dict:
    """Wait for the owner to approve or deny a pending action. Use this instead of polling.

    Blocks (long-poll) and returns as soon as the owner decides, or after timeout_seconds
    (default 120, max 300) with status "pending" and timed_out=true.

    Returns {status: "approved"|"denied"|"pending", reason, payment_token?, timed_out}.
    When status is "approved", call pay(payment_token, merchant, amount) next.
    """
    t = max(1, min(int(timeout_seconds), MAX_WAIT))
    return _call("GET", f"/approvals/{approval_id}/wait", timeout=t + 15, params={"timeout": t})


@mcp.tool()
def pay(payment_token: str, merchant: str, amount: float) -> dict:
    """Pay a merchant from the owner's mock wallet (SIMULATED - no real money moves).

    This is the only way to pay. It needs the one-time payment_token returned by request_action
    (decision "allow") or by wait_for_approval (status "approved"), and merchant and amount must
    exactly match what was authorized. Each token works once and expires after 10 minutes.

    Returns {status: "paid", payment_id, balance_after, ...} or {status: "refused", reason}.
    """
    return _call("POST", "/wallet/pay", json={"token": payment_token, "merchant": merchant, "amount": amount})


@mcp.tool()
def get_mandate(mandate_id: str) -> dict:
    """Get the mandate's limits: allowed merchants and action types, weekly limit,
    approval threshold, expiry, revoked flag, and its Ed25519 signature. Use this to check
    what is permitted before planning purchases."""
    return _call("GET", f"/mandates/{mandate_id}")


@mcp.tool()
def check_approval(approval_id: str) -> dict:
    """Return the current status of an approval immediately, without waiting.
    Prefer wait_for_approval, which blocks until the owner decides."""
    return _call("GET", f"/approvals/{approval_id}")


if __name__ == "__main__":
    mcp.run("stdio")
