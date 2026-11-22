"""Mandate MCP server (stdio) for Claude Code / Claude Desktop.

Exposes request_action, check_approval and get_mandate. Each tool calls the Mandate
FastAPI server at MANDATE_BASE_URL (default http://127.0.0.1:8000), which must be running.

Run (normally launched by the MCP client, not by hand):
    .venv/Scripts/python mcp_server/server.py

stdout is the MCP protocol channel - never print() here; logs go to stderr.
Uses the official MCP Python SDK 2.x, where FastMCP is named MCPServer.
"""
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
logging.basicConfig(stream=sys.stderr, level=logging.INFO)

BASE_URL = os.getenv("MANDATE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

mcp = MCPServer(
    name="mandate",
    instructions=(
        "Mandate is the owner's permission layer. Before ANY purchase or other action that spends "
        "money or acts on the owner's behalf, call request_action. "
        "If the decision is 'allow', proceed. If 'deny', do not proceed; tell the user the reason. "
        "If 'pending_approval', tell the user approval was requested on their phone, then call "
        "check_approval with the approval_id until status is 'approved' (proceed) or 'denied' (stop). "
        "Payments in this project are simulated."
    ),
)


def _call(method: str, path: str, **kwargs) -> dict:
    try:
        r = httpx.request(method, f"{BASE_URL}{path}", timeout=10.0, **kwargs)
    except httpx.HTTPError as e:
        return {"error": f"cannot reach Mandate server at {BASE_URL} ({type(e).__name__}). "
                         "Is it running? Start it with: .venv/Scripts/python -m uvicorn app.main:app"}
    try:
        data = r.json()
    except ValueError:
        data = {"detail": r.text}
    if r.status_code >= 400:
        return {"error": data.get("detail", data), "status_code": r.status_code}
    return data


@mcp.tool()
def request_action(mandate_id: str, action_type: str, merchant: str, amount: float) -> dict:
    """Ask Mandate whether an action is permitted under the owner's mandate.

    Args:
        mandate_id: The mandate the agent acts under (e.g. "mdt_...").
        action_type: Kind of action, e.g. "purchase".
        merchant: Merchant / counterparty, e.g. "amazon.in".
        amount: Amount in the mandate's currency (e.g. INR).

    Returns {decision: allow|deny|pending_approval, reason, approval_id?, action_id}.
    """
    return _call("POST", "/request_action", json={
        "mandate_id": mandate_id, "action_type": action_type, "merchant": merchant, "amount": amount})


@mcp.tool()
def check_approval(approval_id: str) -> dict:
    """Check the owner's decision on a pending action.

    Returns {status: pending|approved|denied, reason, ...}. Only proceed when status is 'approved'.
    """
    return _call("GET", f"/approvals/{approval_id}")


@mcp.tool()
def get_mandate(mandate_id: str) -> dict:
    """Get the mandate's limits: allowed merchants and action types, weekly limit,
    approval threshold, expiry, revoked flag, and its Ed25519 signature."""
    return _call("GET", f"/mandates/{mandate_id}")


if __name__ == "__main__":
    mcp.run("stdio")
