# ai-common-repo

<!-- version-sync -->
> **버전** `0.1.0` · **GitHub** [cjakma/ai-common-repo](https://github.com/cjakma/ai-common-repo) `master` · **API 명세** [docs/API.md](docs/API.md) · **운영** [docs/OPERATIONS.md](docs/OPERATIONS.md)
>
> README 확인일 2026-10-15 · 샌드박스 Phase 6–9 병합 완료(샌드박스 삭제) · 벡터 스토어 = 내장 Qdrant(활성 메모리 493건 = 포인트 493개; 만료된 blacklist-guard 레코드 10,932건 정리 후) · 임베딩: 현재 해시(256차원) → **EmbeddingGemma-300M(768차원) 전환은 2026-10-16 03:30 `aicr-nightly-embed.timer`가 수행** · 테스트 120 passed / 1 skipped, ruff clean
<!-- /version-sync -->

Local-first **shared long-term memory / context gateway** for AI coding agents
(Codex, ClaudeCode, Antigravity-CLI) with vLLM/Ollama as model backends.

> 🤖 **AI agents start here:** read **[manual_for_ai_agent.md](manual_for_ai_agent.md)** —
> how to recall/store shared memory via MCP or REST, with the loop to follow and safety rules.

It adopts Supermemory's *concept* — an AI-facing durable memory layer — but is a
clean-room reimplementation: modular packages, dependency-injected services,
config-driven (no hardcoded tunables), and degrades gracefully offline.

> Design doc: `docs/ai-common-repo_architect.md` (moved here from `~/` on 2026-09-04) · this repo implements
> local SQLite memory + the model-backend abstraction, an async worker/scheduler,
> vector/hybrid search + RAG documents, an MCP server, and polling connectors.

> **Quickstart, examples & the cross-agent shared-memory flow:** see
> **[UseCase.md](UseCase.md)** (REST/CLI samples + per-agent integration).

## Layout

```
packages/
  gateway/ai_common_repo/   # the domain: storage, extraction, resolver, profiles,
                            # search (fts/vector/hybrid), model_backends, workers,
                            # connectors, mcp, observability, server, api
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

**Production** runs as the `ai-common-repo` service in `/home/ubuntu/docker-compose.yml`
(one image = gateway + bundled Qdrant, data on `~/.local/share/ai-common-repo` → `/data`).
Deploy/redeploy with `scripts/deploy.sh` — it tags the running image as
`ai-common-repo:rollback`, rebuilds from this checkout and health-checks `:28787`.
After moving from a non-Qdrant vector backend, run `scripts/backfill_qdrant.py` once
inside the container (`sudo docker exec -i -w /app ai-common-repo python - < scripts/backfill_qdrant.py`).

**Embedding model.** EmbeddingGemma-300M (Q8_0 GGUF) runs as pm2 `embed-gemma`
(`scripts/run-embedding-server.sh`: llama.cpp `--embeddings`, 1 thread, `172.18.0.1:4176`,
ufw allows 172.18.0.0/16 only). `/data/config.yaml` defines it as backend `embed-gemma`
with EmbeddingGemma's query/document prompts and a 2000-char input cut (the model takes
2048 tokens). `scripts/nightly-embed.sh` (systemd `aicr-nightly-embed.timer`, daily 03:30):

- first run: builds collection `ai_common_repo_embgemma768` with `scripts/reembed.py` while
  the gateway keeps serving the old one (~2.3 memories/s on one core), then routes
  `models.tasks.embedding` to `embed-gemma`, points `storage.qdrant_collection` at the new
  collection, restarts, embeds what arrived meanwhile and verifies `/v1/health` — any
  failure restores `config.yaml.bak-pre-switch`. The old `ai_common_repo` collection stays
  for rollback;
- every later run: embeds memories stored while `embed-gemma` was down and prunes points of
  forgotten memories (`reembed.py --prune`).

Embedding failures never fail a request: search falls back to FTS-only results and a store
still succeeds (its vector is filled by the next nightly run). `journalctl -u aicr-nightly-embed`
shows each run.

For local development the CLI runs from a project venv via `-m` (no global install required):

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

## Event subscription / webhooks

The gateway can push domain events out instead of forcing consumers to poll. When
a harness task changes state (`POST /v1/harness/tasks/{id}/status`), a
`harness.task.status_changed` event is published into a **durable SQLite outbox**
right after the transition commits; an async delivery worker fans it out to
config-driven generic **HTTP webhook subscriptions**. Core stays platform-neutral
— there is no Slack/Discord/Hermes code, just signed POSTs to configured URLs.

Guarantees & behaviour:
- **Best-effort / fail-soft**: a webhook or outbox failure never fails the status
  API — the publish is wrapped and, on error, audited as `event_enqueue_failed`.
- **At-least-once** delivery: one outbox row per (event × subscription), each with
  its own delivery id, attempt counter and retry state.
- **Retry/backoff**: `2xx` = delivered; `408/429/5xx`/network error retry with
  exponential backoff + jitter; other `4xx` fail permanently; exhausted attempts
  move to `dead_letter`.
- **HMAC signing**: when a subscription sets `secret_env`, requests carry
  `X-AICR-Signature: v1=<hmac_sha256(secret, "<timestamp>.<body>")>` plus
  `X-AICR-Event-ID / -Event-Type / -Delivery-ID / -Timestamp`. The secret is read
  from the named env var at send time only — never stored or logged.
- **SSRF guard**: only loopback/private webhook hosts are allowed unless
  `allow_external_urls: true`.

Config (default **off**, see `notifications:` in `configs/default.yaml`):

```yaml
notifications:
  enabled: true
  outbox_poll_interval_s: 5
  subscriptions:
    - id: hermes-origin-harness-terminal
      url: http://127.0.0.1:8099/internal/events/harness
      secret_env: AI_COMMON_REPO_WEBHOOK_SECRET   # env var NAME, not the secret
      event_types: [harness.task.status_changed]
      filters:
        executor: [claudecode, openclaw, opencode]
        new_status: [done, failed, cancelled]
        requester: [hermes]
```

Read/test surface:
- `GET /v1/events?status=&event_type=&aggregate_id=&limit=` — outbox rows + counts
- `GET /v1/events/{event_id}` — per-subscription deliveries for one event
- `GET /v1/webhook-subscriptions` — configured subscriptions (secret redacted)
- `POST /v1/webhook-subscriptions/{id}/test` — send a synthetic `webhook.test`

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
  FTS + vector results fused with RRF, then query expansion + feature rerank.
  The vector store is **Qdrant** (`storage.vector_backend: qdrant`; the image sets
  `AICR_STORAGE__QDRANT_URL=http://127.0.0.1:6333` for the bundled server — with no
  URL the client is in-memory, which is for tests only). Brute-force / sqlite-vec
  remain as offline fallbacks. Vectors come from the configured embedding backend
  (EmbeddingGemma-300M, see "Embedding model"), else the deterministic 256-dim hash
  embedding (ASCII n-grams only — it ignores Korean). Documents are chunked, embedded, and
  returned alongside memories with source attribution. FTS queries quote every
  term, so words like `AND` / `OR` / `NOT` / `NEAR` are matched as text.
- **MCP server** (`mcp/`, `POST /mcp`): spec-compliant JSON-RPC over
  Streamable-HTTP exposing 8 tools (`memory_store / memory_search / memory_recall
  / memory_profile / memory_forget / session_ingest` mapped 1:1 to the service, plus
  `harness_request / harness_result` over the `/v1/harness/tasks` queue — live-checked 2026-09-24).
  Ready-to-apply host setups live in `integrations/`.
- **Polling connectors** (`connectors/`, default **off**): GitHub / Notion poll
  into the RAG document pipeline. Tokens are read from env at sync time only and
  never persisted; outbound HTTP is https + host-allow-list + timeout bounded.
- **Event subscription / webhooks** (`events/`, default **off**; enabled in this
  deployment via `/data/config.yaml` for the Hermes harness notifier): durable event
  outbox + async HMAC-signed HTTP delivery with retry/backoff and dead-lettering,
  publishing harness task status changes. See "Event subscription / webhooks".
- **Observability** (`observability/`, read-only): `/v1/health` (liveness +
  readiness, plus `vector_store` — Qdrant collection, point count, vector size — and
  `embedding` — model and backend reachability), `/v1/metrics` (job depth/throughput/failure-rate + search latency,
  JSON or Prometheus), `/v1/jobs` and `/v1/connectors` (queue backlog, recent
  failures, sync watermarks). Structured JSON logging correlates request-id /
  job-id; every surface and log line is redacted (hints, not values). See
  `docs/OPERATIONS.md`.

Storage lives outside the repo at `~/.local/share/ai-common-repo/memory.db`, so
relocating this project (as it was, from `web-service-archive/` to `web-service/`)
does not move or lose data.
