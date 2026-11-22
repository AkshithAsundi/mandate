# Mandate — project context

## Hackathon
- Event: Homingo Hack Days 2026, track A1 "Software built for agents"
  (tools that make agents first-class users: identity, permissions, payments, memory, sandboxes, testing).
- Build phase: Sat 21 Nov 2026 to Sun 6 Dec 2026. Solo builder: Akshith Asundi (GitHub: AkshithAsundi).
- Requirements: public GitHub repo; README states the idea in one sentence and has a section
  listing the AI tools, models and libraries used. AI tools are allowed; the builder reviews all code.

## The idea (one sentence)
Mandate lets you give an AI agent a signed, revocable mandate — what it may do, how much it may
spend, until when — blocks anything outside it, and asks you on your phone for everything in between.

## Who it is for
People who let AI agents act for them, and developers building such agents, who plug in one MCP
server instead of writing their own safety layer.

## Core features
1. Mandates: owner issues a mandate to an agent with spend_limit_per_week, approval_threshold,
   allowed_merchants, allowed_action_types, expires_at, revoked. Each mandate is signed with Ed25519
   so the agent can prove who it acts for.
2. Policy check: every important action goes through request_action and returns
   allow / deny (with reason) / pending_approval.
3. Human approval: pending actions trigger an n8n webhook that sends a Telegram message with
   Approve/Deny; n8n calls back to the server; the agent polls for the decision.
4. Audit log: every request and decision is stored in a hash-chained log (each row holds the hash of
   the previous row); a verify endpoint detects tampering.
5. Revocation: one call revokes a mandate; the agent's next action is refused.

## Sponsors and how we use them
- Olostep: web access for the demo agent (prices, merchant pages).
- Crustdata: verify a counterparty company/merchant exists — usable as a policy rule.
- n8n Cloud: the approval workflow to Telegram.
- Keploy: API tests for the policy server.
- Payments are simulated (mock wallet). Say so honestly in docs.

## Stack
- Python 3.11+, FastAPI, SQLite, pydantic, cryptography (Ed25519), pytest.
- MCP server with FastMCP exposing request_action, check_approval, get_mandate.
- Demo agent: Claude Code / Claude Desktop connected to Mandate's MCP server (stdio). No API keys for models.
- Dashboard: simple web page (later milestone).
- Deploy: Render or Railway (later milestone).

## Repo layout
app/        main.py, db.py, models.py, policy.py, crypto.py, audit.py, approvals.py
mcp_server/ server.py
docs/       claude_mcp_setup.md
dashboard/  (later)
n8n/        exported workflow JSON
tests/      pytest tests

## Rules for working in this repo
- Windows machine. Always use the local .venv (.venv/Scripts/python); never install packages globally. Keep requirements.txt updated.
- Never commit .env or secrets. Read config from .env with python-dotenv.
- Small working steps; after each step, tell me exactly how to run and test it.
- Write pytest tests for every policy rule.
- Keep the README's "AI tools, models and libraries used" section up to date.
- Budget: only free tiers and the hackathon partner credits (Olostep, Crustdata, n8n Cloud, Keploy).
  Never add a paid dependency.

## Secrets
- Keys live only in .env locally, or in host environment variables when deployed.
- Never hardcode, print, log or echo them.
- Never put real keys in tests, README, docs, n8n exports or commits — use placeholders.
- The frontend never holds keys.
- The n8n → POST /approvals/{id} callback must send the shared secret header
  (X-Approval-Secret = APPROVAL_SECRET); the server rejects the callback without it.
- A pre-commit hook (scripts/pre-commit) blocks staged .env files and real-looking secrets.

## Milestones
- M1 (by 23 Nov): end-to-end slice — mandate → request_action → pending_approval → webhook →
  approve callback → agent sees decision → audit log entry.
- M2 (24–29 Nov): full policy engine, MCP server, Telegram via n8n, demo agent doing 2–3 errands.
- M3 (30 Nov–3 Dec): dashboard, Olostep + Crustdata integration, Keploy tests, deploy.
- M4 (4–5 Dec): demo video, pitch, README polish. 6 Dec: buffer only, no new features.

## Demo script (what must work live)
Issue mandate → small purchase allowed → ₹1,200 purchase triggers phone approval → unlisted merchant
blocked with reason → tampered audit log detected → revoke stops the agent mid-task.
