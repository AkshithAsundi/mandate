# Connect Mandate to Claude Code / Claude Desktop (Windows)

Mandate's MCP server (`mcp_server/server.py`) runs over **stdio**. Claude starts it with the
project's venv Python. It exposes five tools that call the Mandate API:

| Tool | What it does |
|---|---|
| `request_action(mandate_id, action_type, merchant, amount)` | Returns `allow` + one-time `payment_token`, `deny` + reason, or `pending_approval` + `approval_id` |
| `wait_for_approval(approval_id, timeout_seconds=120)` | Long-polls until the owner decides. Once approved, it includes the `payment_token` |
| `pay(payment_token, merchant, amount)` | Pays from the mock wallet (simulated). This is the only way to pay |
| `get_mandate(mandate_id)` | Returns the mandate's limits, expiry, revoked flag and signature |
| `check_approval(approval_id)` | Returns the current status without waiting |

No model API key is needed. Claude Code or Claude Desktop is the agent.

> **Security:** run the agent **outside this repo** (any other folder) so it can't read `.env` or
> `APPROVAL_SECRET`. With that secret it could approve its own requests. Register the server at
> **user scope** (below) so the tools work from any folder.

Below, replace `C:\path\to\mandate` with the folder you cloned the repo into.

## 0. Prerequisites (both clients)

```powershell
cd C:\path\to\mandate
python -m venv .venv                       # once
.venv\Scripts\python -m pip install -r requirements.txt
```

The MCP server is a thin client. **The Mandate API must be running** while Claude uses it:

```powershell
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The MCP server finds the API at `MANDATE_BASE_URL` in `.env` (default `http://127.0.0.1:8000`).

Create a mandate to give the agent (PowerShell):

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/mandates -H "Content-Type: application/json" `
  -d '{\"agent_id\":\"claude\",\"owner\":\"me\",\"spend_limit_per_week\":5000,\"approval_threshold\":1000,\"allowed_merchants\":[\"amazon.in\",\"swiggy\"],\"allowed_action_types\":[\"purchase\"],\"expires_at\":\"2026-12-06T23:59:00+05:30\"}'
```

Note the returned `id` (`mdt_...`).

## 1. Claude Code

Register it at user scope so it works from any folder. Forward slashes work fine on Windows:

```powershell
claude mcp add --scope user mandate -- C:/path/to/mandate/.venv/Scripts/python.exe C:/path/to/mandate/mcp_server/server.py
```

If the `claude` CLI isn't installed (for example, you only use the desktop app's Code tab), add the
same entry by hand to `%USERPROFILE%\.claude.json` under the top-level `"mcpServers"` key:

```json
"mandate": {
  "type": "stdio",
  "command": "C:/path/to/mandate/.venv/Scripts/python.exe",
  "args": ["C:/path/to/mandate/mcp_server/server.py"],
  "env": {}
}
```

Then start a Claude Code session **in a folder other than this repo**.

Check it:

```powershell
claude mcp list            # mandate should show as connected
```

In a Claude Code session, `/mcp` also shows the server and its 5 tools.

Try it with a prompt like: *"Using mandate mdt_XXXX, buy a ₹1,200 item from amazon.in."*
1. Claude calls `request_action` and gets `pending_approval` with an `approval_id`.
2. It then calls `wait_for_approval`, which blocks.
3. Approve the request as the owner (see the README's manual flow). The wait returns `approved`
   with a `payment_token`.
4. Claude calls `pay` with that token.

Remove it: `claude mcp remove mandate`.

## 2. Claude Desktop

1. Open Claude Desktop → **Settings → Developer → Edit Config**. This opens
   `claude_desktop_config.json`, usually at `%APPDATA%\Claude\claude_desktop_config.json`.
2. Add the `mandate` entry under `mcpServers`. Keep any servers already listed there.
   In JSON, Windows backslashes must be doubled:

```json
{
  "mcpServers": {
    "mandate": {
      "command": "C:\\path\\to\\mandate\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\mandate\\mcp_server\\server.py"]
    }
  }
}
```

3. **Quit Claude Desktop completely** (right-click the tray icon → Quit) and reopen it.
4. In a new chat, open the tools menu (the slider icon near the input). `mandate` should be listed
   with 3 tools. The first time Claude uses a tool, Desktop asks you to allow it.

## Troubleshooting

- **Tool returns "cannot reach Mandate server"**: the API isn't running. Start uvicorn (step 0).
- **Server not listed / failed to start**: run
  `C:\path\to\mandate\.venv\Scripts\python.exe C:\path\to\mandate\mcp_server\server.py`
  by hand. It should wait silently for input (Ctrl+C to exit). An import error means the venv is
  missing packages; reinstall `requirements.txt`.
- **Claude Desktop logs**: `%APPDATA%\Claude\logs\mcp-server-mandate.log`.
- The MCP server never needs secrets. It reads only `MANDATE_BASE_URL` from `.env`.
  `APPROVAL_SECRET` is only used by the owner side (n8n → `/approvals` callback).
- `wait_for_approval` can block up to `timeout_seconds` (max 300). If your client times out tool
  calls sooner, pass a smaller timeout and call it again.
