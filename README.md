# Mandate

**Mandate lets you give an AI agent a signed, revocable mandate — what it may do, how much it may
spend, until when — blocks anything outside it, and asks you on your phone for everything in between.**

Built for Homingo Hack Days 2026, track A1 "Software built for agents".
The agent is Claude Code or Claude Desktop, connected to Mandate as an MCP server. No model API keys are used.
**Payments are simulated:** a mock wallet with a mock balance. No real money moves.

## Core security property: the agent can only pay with a one-time token

The agent has **no** way to move money except the `pay(token, merchant, amount)` tool, and the
mock wallet behind it accepts nothing but a **one-time authorization token issued by Mandate**:

- A token is issued only when `request_action` returns `allow`, or when the owner approves a
  pending action. Denied actions get no token.
- It is **Ed25519-signed** by the server and **bound** to one mandate, one action, one exact merchant and
  one exact amount, and it **expires after 10 minutes**.
- The wallet checks the signature, that this server issued the token, expiry, exact merchant/amount,
  **single use** (enforced atomically), that the mandate is still valid (not revoked, expired or
  altered), and the balance. Any failure is refused with a clear reason and written to the audit log.

So a misbehaving or prompt-injected agent can't pay more, pay someone else, replay an earlier
payment, or keep spending after you revoke. Policy decisions are enforced at the point of payment
and are more than advice.

### Keep the agent away from the secrets

The guarantee above only holds if the agent can't act as the owner. **The agent must never run with
access to `.env` or `APPROVAL_SECRET`.** With that secret it could approve its own requests.

- Run Claude Code / Claude Desktop **in a different folder** (not this repo), with **MCP-only access**
  to Mandate. The user-scope MCP registration in [docs/claude_mcp_setup.md](docs/claude_mcp_setup.md)
  makes the tools available from any folder.
- The MCP server process reads only `MANDATE_BASE_URL` from `.env`. It never loads `APPROVAL_SECRET`,
  and none of its tools can approve.
- Only the owner side (n8n, or you by hand) holds `APPROVAL_SECRET`.

## What works now (M1 + M2 part A)

- **Mandates** carry: weekly spend limit, approval threshold, allowed merchants, allowed action
  types and expiry. Each is signed with the server's Ed25519 key, which is generated on first run
  in `server_key.pem` (git-ignored). The public key is at `GET /public_key`. A mandate edited in the
  DB fails signature checks and is denied.
- **Policy check**: `POST /request_action` returns `allow`, `deny` + reason, or `pending_approval` + `approval_id`.
  Rules, first match wins: bad signature → revoked → expired → action type → merchant → weekly limit
  (pending approvals reserve budget) → amount above threshold needs approval → allow.
- **Human approval**: pending actions are POSTed to `N8N_WEBHOOK_URL`. If that is empty, the server
  only logs what it would have sent, and the response says no notification channel is configured.
  Pending responses always include the `approval_id`. n8n calls back `POST /approvals/{id}` with the
  `X-Approval-Secret` header. The agent waits with `GET /approvals/{id}/wait?timeout=120`, a long-poll
  that returns as soon as the owner decides (max 300 s). Once approved, the agent's poll includes the
  payment token. The n8n callback response never does.
- **Mock wallet**: `POST /wallet/pay {token, merchant, amount}` and `GET /wallet` (balance and
  recent payments). The starting balance is `MOCK_WALLET_BALANCE` (default 50,000 INR).
- **Revocation**: `POST /mandates/{id}/revoke`. Later actions are denied, pending approvals are
  cancelled, and unused tokens can no longer pay.
- **Audit log**: every request, decision, token issue and payment (paid or refused) goes into a
  hash-chained log. `GET /audit/verify` detects edited or deleted entries.
- **MCP server** (`mcp_server/server.py`) exposes `request_action`, `wait_for_approval`, `pay`,
  `get_mandate` and `check_approval`. The tool descriptions walk the agent through
  request → wait → pay.

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

# 5. Wait as the agent would (long-poll) -> "status":"approved" plus a one-time payment_token
TOK=$(curl -s "http://127.0.0.1:8000/approvals/$APR/wait?timeout=30" \
  | .venv/Scripts/python -c "import sys,json;d=json.load(sys.stdin);print(d['status'],file=sys.stderr);print(d['payment_token'])")

# 6. Pay with the token -> "status":"paid". Wrong amount or a second use is refused.
curl -s -X POST http://127.0.0.1:8000/wallet/pay -H "Content-Type: application/json" -d "{\"token\":\"$TOK\",\"merchant\":\"amazon.in\",\"amount\":9999}"; echo
curl -s -X POST http://127.0.0.1:8000/wallet/pay -H "Content-Type: application/json" -d "{\"token\":\"$TOK\",\"merchant\":\"amazon.in\",\"amount\":1200}"; echo
curl -s -X POST http://127.0.0.1:8000/wallet/pay -H "Content-Type: application/json" -d "{\"token\":\"$TOK\",\"merchant\":\"amazon.in\",\"amount\":1200}"; echo
curl -s http://127.0.0.1:8000/wallet; echo

# 7. Verify the audit chain -> {"valid":true,...}
curl -s http://127.0.0.1:8000/audit/verify; echo

# 8. Revoke -> the next action is denied
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
app/          FastAPI server: main.py, db.py, models.py, policy.py, crypto.py, audit.py,
              approvals.py, tokens.py (payment tokens), wallet.py (mock wallet), clock.py, config.py
mcp_server/   server.py — MCP server (stdio) for Claude Code / Claude Desktop
docs/         claude_mcp_setup.md
scripts/      pre-commit
tests/        pytest tests (policy rules, approvals, audit tampering, wallet/token enforcement)
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
