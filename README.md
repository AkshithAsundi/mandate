# Mandate

**Mandate lets you give an AI agent a signed, revocable mandate — what it may do, how much it may
spend, until when — blocks anything outside it, and asks you on your phone for everything in between.**

Built for Homingo Hack Days 2026, track A1 "Software built for agents".
The agent is Claude Code or Claude Desktop, connected to Mandate as an MCP server. No model API keys are used.
**Payments are simulated:** Mandate decides whether an action may happen. No real money moves.

## What works now (Milestone M1)

- **Mandates** carry: weekly spend limit, approval threshold, allowed merchants, allowed action
  types and expiry. Each is signed with the server's Ed25519 key, which is generated on first run
  in `server_key.pem` (git-ignored). The public key is at `GET /public_key`. A mandate edited in the
  DB fails signature checks and is denied.
- **Policy check**: `POST /request_action` returns `allow`, `deny` + reason, or `pending_approval` + `approval_id`.
  Rules, first match wins: bad signature → revoked → expired → action type → merchant → weekly limit
  (pending approvals reserve budget) → amount above threshold needs approval → allow.
- **Human approval**: pending actions are POSTed to `N8N_WEBHOOK_URL`. If that is empty, the server
  only logs what it would have sent. n8n calls back `POST /approvals/{id}` with the
  `X-Approval-Secret` header. The agent polls `GET /approvals/{id}`.
- **Revocation**: `POST /mandates/{id}/revoke`. Later actions are denied, and pending approvals are cancelled.
- **Audit log**: every request and decision goes into a hash-chained log. `GET /audit/verify` detects
  edited or deleted entries.
- **MCP server** (`mcp_server/server.py`) exposes `request_action`, `check_approval` and `get_mandate`.

## Run it (Windows)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env          # then set APPROVAL_SECRET to a long random value
cp scripts/pre-commit .git/hooks/pre-commit   # secret-leak guard (see below)
```

Run the tests:

```bash
.venv/Scripts/python -m pytest -q
```

Start the API (interactive docs at http://127.0.0.1:8000/docs):

```bash
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

To connect Claude Code or Claude Desktop, see [docs/claude_mcp_setup.md](docs/claude_mcp_setup.md).

### Manual end-to-end test (Git Bash, with the API running)

```bash
# 1. Create a mandate (₹5,000/week, approval above ₹1,000)
MID=$(curl -s -X POST http://127.0.0.1:8000/mandates -H "Content-Type: application/json" \
  -d '{"agent_id":"claude","owner":"me","spend_limit_per_week":5000,"approval_threshold":1000,"allowed_merchants":["amazon.in","swiggy"],"allowed_action_types":["purchase"],"expires_at":"2026-12-06T23:59:00+05:30"}' \
  | .venv/Scripts/python -c "import sys,json;print(json.load(sys.stdin)['id'])"); echo $MID

# 2. Small purchase -> allow;  unlisted merchant -> deny
curl -s -X POST http://127.0.0.1:8000/request_action -H "Content-Type: application/json" -d "{\"mandate_id\":\"$MID\",\"action_type\":\"purchase\",\"merchant\":\"swiggy\",\"amount\":300}"; echo
curl -s -X POST http://127.0.0.1:8000/request_action -H "Content-Type: application/json" -d "{\"mandate_id\":\"$MID\",\"action_type\":\"purchase\",\"merchant\":\"evil.biz\",\"amount\":50}"; echo

# 3. ₹1,200 purchase -> pending_approval
APR=$(curl -s -X POST http://127.0.0.1:8000/request_action -H "Content-Type: application/json" \
  -d "{\"mandate_id\":\"$MID\",\"action_type\":\"purchase\",\"merchant\":\"amazon.in\",\"amount\":1200}" \
  | .venv/Scripts/python -c "import sys,json;d=json.load(sys.stdin);print(d['decision'],file=sys.stderr);print(d['approval_id'])"); echo $APR

# 4. Approve as n8n would (secret read from .env, never printed). Without the header -> 401.
curl -s -X POST http://127.0.0.1:8000/approvals/$APR -H "Content-Type: application/json" -d '{"decision":"approve"}'; echo
curl -s -X POST http://127.0.0.1:8000/approvals/$APR -H "Content-Type: application/json" \
  -H "X-Approval-Secret: $(grep '^APPROVAL_SECRET=' .env | cut -d= -f2-)" -d '{"decision":"approve"}'; echo

# 5. Poll as the agent would -> "status":"approved"
curl -s http://127.0.0.1:8000/approvals/$APR; echo

# 6. Verify the audit chain -> {"valid":true,...}
curl -s http://127.0.0.1:8000/audit/verify; echo

# 7. Revoke -> the next action is denied
curl -s -X POST http://127.0.0.1:8000/mandates/$MID/revoke > /dev/null
curl -s -X POST http://127.0.0.1:8000/request_action -H "Content-Type: application/json" -d "{\"mandate_id\":\"$MID\",\"action_type\":\"purchase\",\"merchant\":\"swiggy\",\"amount\":10}"; echo
```

To demo tamper detection, stop the server and run
`.venv/Scripts/python -c "import sqlite3;c=sqlite3.connect('mandate.db');c.execute(\"UPDATE audit_log SET payload=replace(payload,'300','3') WHERE seq=2\");c.commit()"`.
Then start it again and call `/audit/verify`. It returns `"valid":false` with `broken_at_seq`.

## Secrets

- Secrets live only in `.env` (git-ignored) or in host env vars. `.env.example` has empty placeholders.
- `POST /approvals/{id}` requires `X-Approval-Secret` = `APPROVAL_SECRET`. If the server has no
  secret set, it refuses every callback.
- **Pre-commit hook** (`scripts/pre-commit`, plain bash): blocks commits that stage a `.env` file,
  an Anthropic-style key, a Telegram bot token, or a `*KEY=` / `*TOKEN=` / `*SECRET=` line with a
  real-looking value. Install it once per clone:

  ```bash
  cp scripts/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
  ```

## Layout

```
app/          FastAPI server: main.py, db.py, models.py, policy.py, crypto.py, audit.py, approvals.py, config.py
mcp_server/   server.py — MCP server (stdio) for Claude Code / Claude Desktop
docs/         claude_mcp_setup.md
scripts/      pre-commit
tests/        pytest tests (policy rules, approvals, audit tampering)
```

## AI tools, models and libraries used

**AI tools / models**
- **Claude Code** (Anthropic), running Claude Opus 5.5: coding assistant used to write this code.
  The builder reviews every change.
- **Claude Code / Claude Desktop**: the demo agent. It connects to Mandate through MCP.
  No model API is called from this repo.

**Libraries** (all free / open source)
- [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/): HTTP API
- [Pydantic](https://docs.pydantic.dev/): request and response validation
- [cryptography](https://cryptography.io/): Ed25519 mandate signatures
- [python-dotenv](https://github.com/theskumar/python-dotenv): loads `.env`
- [HTTPX](https://www.python-httpx.org/): n8n webhook and the MCP server's calls to the API
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp` 2.x, `MCPServer`, formerly FastMCP): MCP server
- [pytest](https://pytest.org/): tests
- SQLite (Python standard library): storage

**Services (planned, free tiers / hackathon credits only)**
- n8n Cloud (approval workflow → Telegram), Olostep, Crustdata, Keploy.
