# ai-common-repo

Local-first **shared long-term memory / context gateway** for AI coding agents
(Codex, ClaudeCode, Antigravity-CLI) with vLLM/Ollama as model backends.

> 🤖 **AI agents start here:** read **[manual_for_ai_agent.md](manual_for_ai_agent.md)** —
> how to recall/store shared memory via MCP or REST, with the loop to follow and safety rules.

It adopts Supermemory's *concept* — an AI-facing durable memory layer — but is a
clean-room reimplementation: modular packages, dependency-injected services,
config-driven (no hardcoded tunables), and degrades gracefully offline.

> Design doc: `/home/ubuntu/ai-common-repo_architect.md` · this repo implements
> local SQLite memory + the model-backend abstraction, an async worker/scheduler,
> vector/hybrid search + RAG documents, an MCP server, and polling connectors.

> **Quickstart, examples & the cross-agent shared-memory flow:** see
> **[UseCase.md](UseCase.md)** (REST/CLI samples + per-agent integration).

## Layout

```
packages/
  gateway/ai_common_repo/   # the domain: storage, extraction, resolver, profiles,
                            # search (fts/vector/hybrid), model_backends, workers,
                            # connectors, mcp, server, api
  cli/ai_common_repo_cli/   # `ai-common-repo` CLI (talks to the local service)
  adapters/                 # codex-memory / claudecode-memory / antigravity-memory
configs/default.yaml        # every tunable lives here (override via ~/.config or AICR_*)
integrations/               # ready-to-apply MCP host setups (claude-code/opencode/
                            # openclaw/hermes) — apply by hand, never auto-applied
schemas/                    # JSON schemas for session/memory/profile
```

Architecture invariants:
- `MemoryService` is the single composition root; HTTP/CLI/MCP are thin shells.
- Model providers sit behind `model_backends.base` Protocols and are selected by
  config per task (`extraction`, `summarization`, ...). vLLM/Ollama are just
  OpenAI-compatible backends.
- Adapters never patch vendor binaries; ClaudeCode/Antigravity integrate via
  their existing wrapper's pre/post hooks; Codex via prompt-prefix; any
  MCP-capable client via the `/mcp` Streamable-HTTP endpoint.
- Secret redaction (config patterns) runs before persistence and before any
  external model call. All outbound HTTP (model backends, connectors) is
  timeout-bounded and (for connectors) https + host-allow-list restricted.
- Replaceable infrastructure sits behind interfaces — `JobQueue`, `VectorIndex`,
  `EmbeddingBackend`, `ChatBackend`, `Connector` — selected by config; all SQL
  stays in the storage layer.

## Install & run

This deployment runs from a project venv via `-m` (no global install required):

```bash
cd /home/ubuntu/web-service/ai-common-repo
export PYTHONPATH=packages/gateway:packages/cli:packages/adapters

# gateway — bind 0.0.0.0 so containers (e.g. ccacw) can reach it; default is 127.0.0.1
./.venv/bin/python -m ai_common_repo_cli serve --host 0.0.0.0 --port 28787

./.venv/bin/python -m ai_common_repo_cli store "this project uses uv" --type decision
./.venv/bin/python -m ai_common_repo_cli recall "build rules"
./.venv/bin/python -m ai_common_repo_cli search "deploy" --mode hybrid   # fts/vector/hybrid
./.venv/bin/python -m ai_common_repo_cli ingest-doc --file NOTES.md       # RAG document ingest
./.venv/bin/python -m ai_common_repo_cli context --cwd "$PWD" --tool codex
./.venv/bin/python -m ai_common_repo_cli model-backends health

# run the async worker standalone (extraction / profile rebuild / forget / connector polling)
./.venv/bin/python -m ai_common_repo_cli worker
```

Alternatively `pip install -e .` to expose the `ai-common-repo` / `codex-memory` /
`claudecode-memory` console scripts on PATH (then you can drop the `python -m`
prefix and PYTHONPATH).

## Use with the ClaudeCode / Antigravity wrapper (ccacw)

The OpenAI-compatible wrapper for Claude Code + Antigravity CLI —
[**cjakma/claude-code-antigravity-cli-wrapper**](https://github.com/cjakma/claude-code-antigravity-cli-wrapper)
(`ccacw`, runs via Docker Compose) — ships a **fail-soft Node memory hook**
(`src/memory/gateway.ts`) that calls this gateway: **recall** before spawning the
CLI (merged into the system prompt) and **ingest** after the turn
(fire-and-forget). It is opt-in and never breaks the wrapped tool if the gateway
is unreachable.

**1. Run this gateway reachable from the wrapper container.** Bind `0.0.0.0`; a
container on a user-defined docker network reaches the host at that network's
bridge gateway IP (e.g. `172.18.0.1:28787`) — `host.docker.internal` maps to
`docker0` and is *not* reachable from user networks. Allow only that subnet
through the host firewall:

```bash
sudo ufw allow from 172.18.0.0/16 to any port 28787 proto tcp
```

**2. Wire it into the wrapper's `docker-compose.yml`** (the `ccacw` service):

```yaml
services:
  ccacw:
    environment:
      AICR_GATEWAY_URL: "${AICR_GATEWAY_URL:-}"        # empty = hook disabled (zero calls)
      AICR_MEMORY_ENABLED: "${AICR_MEMORY_ENABLED:-true}"
      AICR_MEMORY_TIMEOUT_MS: "${AICR_MEMORY_TIMEOUT_MS:-4000}"
      AICR_MEMORY_RECALL_LIMIT: "${AICR_MEMORY_RECALL_LIMIT:-8}"
      AICR_USER_ID: "${AICR_USER_ID:-}"
      AICR_PROJECT_ID: "${AICR_PROJECT_ID:-}"
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

**3. Enable + bring it up:**

```bash
echo 'AICR_GATEWAY_URL=http://172.18.0.1:28787' >> .env   # the ccacw network's host bridge IP
docker compose up -d --build ccacw
```

Leave `AICR_GATEWAY_URL` empty to keep the hook dormant; set it and every chat
turn shares memory through this gateway. See [UseCase.md](UseCase.md) §6 for the
per-agent integration details.

## Config precedence

`configs/default.yaml` → `~/.config/ai-common-repo/config.yaml` (or `$AICR_CONFIG`)
→ `AICR_*` env scalars (`AICR_SERVER__PORT=29000`).

## Status

Core memory is **live** and wired into the ClaudeCode/Antigravity wrapper
(`ccacw`) through a fail-soft Node memory hook (recall on pre, ingest on post):
store / recall / ingest+heuristic extraction / conflict-dedupe / profile / search
/ context block are verified against the running gateway. Model extraction
activates automatically when a configured backend is reachable; otherwise a
transparent heuristic runs.

Built on top of that (test-covered, degrade-gracefully offline):
- **Async worker + scheduler** (`workers/`): SQLite-backed `JobQueue`, in-process
  worker with retry/backoff and idempotency, interval scheduler. Extraction,
  profile rebuild, auto-forget (expiry/decay), and connector polling run as jobs.
- **Vector / hybrid search + RAG documents** (`search/`, `/v1/documents`):
  FTS + vector results fused with RRF; deterministic hash-embedding fallback so
  vector search works with no embedding backend. Documents are chunked, embedded,
  and returned alongside memories with source attribution.
- **MCP server** (`mcp/`, `POST /mcp`): spec-compliant JSON-RPC over
  Streamable-HTTP exposing 6 tools (`memory_store / memory_search / memory_recall
  / memory_profile / memory_forget / session_ingest`) mapped 1:1 to the service.
  Ready-to-apply host setups live in `integrations/`.
- **Polling connectors** (`connectors/`, default **off**): GitHub / Notion poll
  into the RAG document pipeline. Tokens are read from env at sync time only and
  never persisted; outbound HTTP is https + host-allow-list + timeout bounded.

Storage lives outside the repo at `~/.local/share/ai-common-repo/memory.db`, so
relocating this project (as it was, from `web-service-archive/` to `web-service/`)
does not move or lose data.
