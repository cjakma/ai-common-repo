# ai-common-repo — Use Cases & Quickstart

A practical, copy-paste guide to using **ai-common-repo** as a shared long-term
memory / context layer across multiple AI coding agents (Codex, ClaudeCode,
Antigravity-CLI) backed by local SQLite, with vLLM/Ollama as optional model
backends.

> The headline: **several agents, one project memory.** Agent A stores a fact,
> Agent B ingests a session, Agent C recalls the shared context — all keyed by
> `project_id` / `user_id`, with no vendor lock-in.

---

## 1. Start the gateway

Run from the project venv (no global install needed):

```bash
cd /home/ubuntu/web-service/ai-common-repo
export PYTHONPATH=packages/gateway:packages/cli:packages/adapters
./.venv/bin/python -m ai_common_repo_cli serve --host 0.0.0.0 --port 28787
```

Or use the systemd service (boot-persistent):

```bash
sudo systemctl enable --now ai-common-repo.service
curl -s http://127.0.0.1:28787/healthz        # {"status":"ok"}
```

Storage lives at `~/.local/share/ai-common-repo/memory.db` (outside the repo,
so relocating the project never loses data).

---

## 2. Core concepts

| Concept | Meaning |
|---|---|
| `memory` | One durable fact. Has `type` (preference/constraint/decision/workflow/project_fact/warning/credential_hint/handoff/todo_context) and `scope` (user/project/repo/global). |
| `project_id` | Routing key. Derive it from the git remote / repo path (`ai-common-repo project-id detect`). Shared by all agents working the same repo. |
| `profile` | Auto-built stable/dynamic summary of a user/project from active memories. |
| `context_block` | Ready-to-inject markdown of profile + relevant memories, for a prompt prefix or system prompt. |

---

## 3. REST API (curl)

```bash
G=http://127.0.0.1:28787
PROJ=repo:github.com/acme/widget

# store an explicit memory
curl -s -X POST $G/v1/memory -H 'Content-Type: application/json' -d '{
  "content": "This repo uses uv for Python deps and ruff for lint.",
  "type": "constraint", "project_id": "'$PROJ'"
}'

# ingest a finished session (auto-extracts durable memories + rebuilds profile)
curl -s -X POST $G/v1/sessions/ingest -H 'Content-Type: application/json' -d '{
  "schema": "ai-common-repo.session.v1", "tool": "claudecode", "project_id": "'$PROJ'",
  "messages": [{"role":"user","content":"We must never commit secrets; deploy via docker compose."}]
}'

# recall context for the current task (returns profile + memories + context_block)
curl -s -X POST $G/v1/recall -H 'Content-Type: application/json' -d '{
  "query": "project setup and rules", "project_id": "'$PROJ'", "tool": "codex"
}'

# search — mode is fts (default) / vector / hybrid; results fuse memories + doc chunks
curl -s -X POST $G/v1/search -H 'Content-Type: application/json' -d '{
  "q": "deploy", "project_id": "'$PROJ'", "mode": "hybrid", "limit": 10
}'

# ingest a RAG document (text/markdown/code) — chunked, embedded, searchable
curl -s -X POST $G/v1/documents -H 'Content-Type: application/json' -d '{
  "content": "# Runbook\nDeploys go through docker compose; never push to prod on Friday.",
  "source_type": "markdown", "title": "Runbook", "project_id": "'$PROJ'"
}'

# profile, forget, backend health
curl -s "$G/v1/profile?project_id=$PROJ"
curl -s -X POST $G/v1/memory/<mem_id>/forget
curl -s $G/v1/model-backends/health
```

**Search modes.** `fts` is exact full-text (whole-word). `vector` retrieves by
semantic/lexical similarity via embeddings (with a deterministic hash-embedding
fallback when no embedding backend is configured), so it recalls items FTS misses
(e.g. `postgres` ↔ `postgresql`). `hybrid` fuses both with Reciprocal Rank Fusion.
All modes return memories **and** ingested document chunks, tagged by source.

---

## 4. CLI

```bash
alias acr='./.venv/bin/python -m ai_common_repo_cli'   # PYTHONPATH must be set

acr store "Always run ruff before committing." --type constraint --project-id repo:acme/widget
acr recall "build rules" --project-id repo:acme/widget
acr search "ruff" --mode hybrid --project-id repo:acme/widget   # fts/vector/hybrid
acr ingest-doc --file RUNBOOK.md --project-id repo:acme/widget  # or --path / - (stdin)
acr context --cwd "$PWD" --tool codex          # prints an injectable context block
acr project-id detect --cwd "$PWD"             # repo: / cwd: fingerprint
acr model-backends health
acr worker                                     # run the async worker standalone
```

---

## 5. Shared memory across agents (the point)

Same `project_id` ⇒ shared memory. Each agent reads/writes the same store:

```bash
PROJ=demo:shared

# Agent A (Codex) records a durable constraint
curl -s -X POST $G/v1/memory -d '{"content":"Repo uses uv + ruff.","type":"constraint","project_id":"'$PROJ'"}' -H 'Content-Type: application/json'

# Agent B (ClaudeCode) ingests its session — extraction adds a memory
curl -s -X POST $G/v1/sessions/ingest -d '{"schema":"ai-common-repo.session.v1","tool":"claudecode","project_id":"'$PROJ'","messages":[{"role":"user","content":"Never commit secrets; deploy via docker compose."}]}' -H 'Content-Type: application/json'

# Agent C (Antigravity-CLI) recalls the combined context
curl -s -X POST $G/v1/recall -d '{"query":"project rules","project_id":"'$PROJ'","tool":"antigravity-cli"}' -H 'Content-Type: application/json'
#  -> memories: "Repo uses uv + ruff." + "Never commit secrets..." + a context_block
```

This is exactly the verified flow: **store(codex) → ingest(claudecode) → recall(antigravity)** returns the shared project memory.

---

## 6. Per-agent integration

Adapters are thin and **never patch vendor binaries**. Each agent integrates at
its natural seam.

### Codex — prompt-prefix wrapper
```bash
codex-memory "implement the feature"     # recalls context, prepends it, runs codex, ingests the session
codex-memory --no-memory "..."           # bypass
codex-memory --store "this project uses uv"
```

### ClaudeCode / Antigravity-CLI — existing wrapper hooks
They already run behind a wrapper, so add pre/post hooks (no binary edits):
```bash
# pre: write a context block for the wrapper to inject
claudecode-memory pre  --cwd "$PWD" --argv-json "$ARGV_JSON" --out-context "$CTX"
# post: ingest the finished session
claudecode-memory post --cwd "$PWD" --session-id "$SID" --exit-code "$?" --stdout "$LOG" --diff "$DIFF"

antigravity-memory pre  --run-id "$RUN_ID" --task "$TASK" --out-context "$CTX"
antigravity-memory post --run-id "$RUN_ID" --exit-code "$?" --log "$LOG" --agent-id "$AGENT"
```

### Live deployment example — the ccacw wrapper (Node)
The Claude/Antigravity OpenAI-compatible wrapper (`ccacw`, a container) integrates
via a **fail-soft Node hook** that calls the gateway on each turn:
`recall` before spawning the CLI (merged into the system prompt) and `ingest`
after (fire-and-forget). It is opt-in (`AICR_GATEWAY_URL`) and never breaks the
wrapped tool if the gateway is down. See `src/memory/gateway.ts` in that project.

### Any other tool — MCP (`POST /mcp`)
The gateway serves a spec-compliant JSON-RPC MCP endpoint over Streamable-HTTP at
`POST /mcp`, exposing six tools mapped 1:1 to the service: `memory_store /
memory_search / memory_recall / memory_profile / memory_forget / session_ingest`.
Any MCP-capable client shares the same memory:

```bash
# list the tools
curl -s -X POST $G/mcp -H 'Content-Type: application/json' -d '{
  "jsonrpc": "2.0", "id": 1, "method": "tools/list"
}'

# call one
curl -s -X POST $G/mcp -H 'Content-Type: application/json' -d '{
  "jsonrpc": "2.0", "id": 2, "method": "tools/call",
  "params": {"name": "memory_recall", "arguments": {"query": "project rules", "project_id": "demo:shared"}}
}'
```

Ready-to-apply host setups (config snippets + `APPLY.md` with rollback) live in
`integrations/` for **claude-code**, **opencode**, **openclaw**, and **hermes**.
They are templates applied by hand after review — nothing auto-applies.

---

## 7. Model backends (optional)

Extraction/summarization run on a configured backend when reachable; otherwise a
transparent heuristic runs (so the gateway is useful fully offline). Backends are
OpenAI-compatible and selected per task in `configs/default.yaml`:

```yaml
models:
  default_backend: local-ollama
  backends:
    local-ollama: { provider: openai_compatible, base_url: http://127.0.0.1:11434/v1, api_key: ollama, chat_model: qwen2.5-coder:14b }
    local-vllm:   { provider: openai_compatible, base_url: http://127.0.0.1:8000/v1,  api_key: EMPTY,  chat_model: Qwen/Qwen2.5-Coder-14B-Instruct }
  tasks: { extraction: local-ollama, summarization: local-ollama }
```

The client sends `max_completion_tokens` with a `max_tokens` fallback, so it
works with both legacy and GPT-5-era endpoints.

---

## 8. Polling connectors (GitHub / Notion, optional)

Connectors poll an external source on the scheduler and feed normalized documents
into the same RAG pipeline (chunk → embed → search), so connector content shows up
in `search` / `recall` alongside memories. **Disabled by default.** Enable in
config and run the worker (`acr worker`, or the gateway with `workers.enabled`):

```yaml
connectors:
  github:
    enabled: true
    token_env: GITHUB_TOKEN        # token read from env at sync time only; never stored
    base_url: https://api.github.com
    repos: ["acme/widget"]
    project_id: repo:acme/widget
    poll_interval_s: 900
  notion:
    enabled: true
    token_env: NOTION_TOKEN
    databases: ["<database_id>"]
```

Security: the token lives only in the environment and is sent as a Bearer header at
request time — it is never written to the DB, logs, or cursors (only a sync
watermark is persisted). Outbound calls are https-only, restricted to the
configured host, and timeout-bounded; re-polling is idempotent (unchanged content
is skipped, changed content replaces the old chunks).

---

## 9. Configuration & deployment

- Config precedence: `configs/default.yaml` → `~/.config/ai-common-repo/config.yaml` → `AICR_*` env (`AICR_SERVER__PORT=29000`).
- Bind `0.0.0.0` to let containers reach the gateway. From a container on a
  user-defined docker network, reach the host at its bridge gateway IP
  (e.g. `http://172.18.0.1:28787`) and allow it through the host firewall for
  that subnet only.
- Secret redaction (config regex patterns) runs **before** persistence and
  **before** any external model call; sensitive content is never stored — only a
  hint.

---

## 10. Privacy

```yaml
privacy:
  redact_secrets: true
  external_model_requires_redaction: true
  store_raw_outputs: false
  secret_patterns: ["sk-...", "ghp_...", "xox[baprs]-...", "AKIA[0-9A-Z]{16}", "-----BEGIN .*PRIVATE KEY-----"]
```

Bad: `GitHub token is ghp_xxx`. Good (what gets stored): *"Project requires a
GitHub token from the configured env file; never store the value."*
