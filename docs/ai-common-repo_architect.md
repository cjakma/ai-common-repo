# ai-common-repo Architecture

작성일: 2026-06-25  
대상: Codex, ClaudeCode wrapper, Antigravity-CLI wrapper, vLLM, Ollama  
목표: Supermemory의 개념만 차용해, 에러 많은 기존 Supermemory GitHub 코드를 참고하지 않고 새로 설계하는 AI 공용 장기기억/컨텍스트 레이어

---

## 1. 설계 목표

`ai-common-repo`는 여러 AI 도구가 같은 장기기억과 프로젝트 컨텍스트를 공유하도록 하는 로컬 우선 memory gateway이다.

지원 대상:

- Codex
- ClaudeCode
- Antigravity-CLI
- vLLM OpenAI-compatible server
- Ollama OpenAI-compatible/local API

핵심 요구사항:

1. 대화에서 중요한 사실 자동 추출
2. 사용자 프로필 자동 생성/갱신
3. 오래된 정보와 모순된 정보 처리
4. 프로젝트별 memory scope 분리
5. 문서/RAG + 개인 memory 검색 통합
6. MCP 서버로 여러 AI 도구에서 같은 기억 공유
7. API/SDK/CLI/local self-host 방식 지원
8. ClaudeCode와 Antigravity-CLI는 이미 wrapper로 동작 중이므로 vendor CLI를 직접 수정하지 않음
9. vLLM/Ollama는 memory extraction, summarization, embedding 또는 chat completion backend로 사용할 수 있음

비목표:

- 기존 `supermemoryai/supermemory` monorepo clone/build 의존
- ClaudeCode/Antigravity vendor binary patching
- 원본 로그를 무제한 저장하는 감시 도구
- 단순 vector DB wrapper만 제공하는 RAG-only 시스템

---

## 2. 전체 아키텍처

```txt
사용자
  │
  ├─ codex
  │    └─ codex-memory-adapter
  │
  ├─ claudecode wrapper
  │    └─ claudecode-memory-hook
  │
  ├─ antigravity-cli wrapper
  │    └─ antigravity-memory-hook
  │
  ├─ vLLM OpenAI-compatible endpoint
  │    └─ model-backend-provider
  │
  └─ Ollama local/OpenAI-compatible endpoint
       └─ model-backend-provider

          │
          ▼

  ai-common-repo memory-gateway daemon
  ├─ context prefetch
  ├─ memory extraction
  ├─ session ingest
  ├─ project/user scope routing
  ├─ contradiction/update resolver
  ├─ search API
  ├─ profile builder
  ├─ MCP server
  ├─ OpenAI-compatible model client
  └─ persistence layer

          │
          ▼

  storage
  ├─ SQLite metadata
  ├─ SQLite FTS5
  ├─ optional sqlite-vec / LanceDB / Qdrant
  ├─ raw sessions
  ├─ extracted memories
  ├─ user/project profiles
  ├─ document chunks
  └─ audit log
```

핵심 원칙:

- CLI별 adapter는 얇게 유지한다.
- 장기기억 로직은 `memory-gateway`에 집중한다.
- ClaudeCode와 Antigravity-CLI는 기존 wrapper의 pre/post hook으로 통합한다.
- vLLM/Ollama는 `model backend`로 추상화하여 extraction/profile/search 단계에서 선택적으로 사용한다.
- 처음에는 SQLite + FTS5로 시작하고, vector/graph는 옵션으로 분리한다.

---

## 3. Repository Layout

```txt
ai-common-repo/
  README.md
  pyproject.toml
  uv.lock
  configs/
    default.yaml
    examples/
      local-ollama.yaml
      vllm-server.yaml
      privacy-strict.yaml
  packages/
    gateway/
      ai_common_repo/
        server/
        api/
        storage/
        search/
        extraction/
        resolver/
        profiles/
        model_backends/
        mcp/
        privacy/
        project_scope/
    adapters/
      codex_memory/
      claudecode_hook/
      antigravity_hook/
    cli/
      ai_common_repo_cli/
  schemas/
    memory.schema.json
    session.schema.json
    profile.schema.json
  scripts/
    install-wrapper-hooks.sh
    migrate-db.sh
  docs/
    architecture.md
    wrapper-integration.md
    codex.md
    claudecode.md
    antigravity-cli.md
    vllm.md
    ollama.md
    privacy.md
    mcp.md
```

---

## 4. 주요 컴포넌트

### 4.1 memory-gateway

중앙 장기기억 daemon이다.

실행 예:

```bash
ai-common-repo serve --host 127.0.0.1 --port 28787
```

역할:

- session ingest
- recall/context prefetch
- explicit memory store/forget
- memory candidate extraction
- profile rebuild
- FTS/vector hybrid search
- conflict/supersede 처리
- MCP server 제공
- vLLM/Ollama/OpenAI-compatible model backend 호출

---

### 4.2 Codex Adapter

Codex는 아래 우선순위로 통합한다.

1. Codex가 MCP를 안정적으로 지원하면 `ai-common-repo` MCP server 연결
2. MCP가 어렵거나 제한되면 `codex-memory` wrapper 사용
3. 최후에는 prompt-prefix context injection 사용

예:

```bash
codex-memory "이 기능 구현해줘"
```

동작:

```txt
1. cwd, git root, branch, remote 감지
2. project_id 계산
3. memory-gateway /v1/recall 호출
4. context block 생성
5. 원래 codex 실행
6. stdout/stderr/session metadata capture
7. /v1/sessions/ingest 호출
```

---

### 4.3 ClaudeCode Hook

중요: ClaudeCode는 이미 wrapper로 동작 중이라는 전제를 지킨다.

설계:

```txt
사용자
  │
  ▼
기존 claudecode wrapper
  │
  ├─ pre_hook: claudecode-memory pre
  ├─ original claude command invoke
  └─ post_hook: claudecode-memory post
```

권장 hook interface:

```bash
claudecode-memory pre \
  --cwd "$PWD" \
  --argv-json "$ARGV_JSON" \
  --out-context "$CTX_FILE"

claudecode-memory post \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  --exit-code "$EXIT_CODE" \
  --stdout "$STDOUT_LOG" \
  --stderr "$STDERR_LOG" \
  --diff "$DIFF_FILE"
```

주의:

- ClaudeCode binary를 직접 수정하지 않는다.
- repo의 `CLAUDE.md`를 영구 변경하지 않는다.
- context는 temp file, prompt-prefix, wrapper-supported system prompt 방식 중 가능한 방식으로 주입한다.
- wrapper에서 이미 session/log handling을 한다면 해당 log path만 post hook에 전달한다.

---

### 4.4 Antigravity-CLI Hook

중요: Antigravity-CLI도 이미 wrapper로 동작 중이라는 전제를 지킨다.

설계:

```txt
사용자
  │
  ▼
기존 antigravity-cli wrapper
  │
  ├─ pre_hook: antigravity-memory pre
  ├─ original antigravity command invoke
  └─ post_hook: antigravity-memory post
```

Antigravity가 multi-agent/autonomous run일 수 있으므로 session schema에 다음을 추가한다.

- `run_id`
- `task_id`
- `agent_id`
- `parent_run_id`
- `delegation_depth`
- `worktree_path`

예:

```bash
antigravity-memory pre \
  --run-id "$RUN_ID" \
  --task "$TASK" \
  --cwd "$PWD" \
  --out-context "$RUN_CONTEXT"

antigravity-memory post \
  --run-id "$RUN_ID" \
  --exit-code "$EXIT_CODE" \
  --log "$LOG_FILE" \
  --diff "$DIFF_FILE" \
  --artifacts "$ARTIFACTS_JSON"
```

주의:

- double wrapping을 피하고 기존 wrapper 내부 hook만 사용한다.
- 긴 실행 로그는 raw retention policy를 적용한다.
- 요약본과 durable memory candidate를 분리한다.

---

### 4.5 vLLM Backend

vLLM은 `ai-common-repo`의 model backend 중 하나다. 직접 memory tool처럼 쓰기보다는 다음 용도로 사용한다.

- memory candidate extraction
- session summarization
- profile rebuild
- contradiction classification
- optional reranking
- OpenAI-compatible chat completion backend

vLLM은 OpenAI-compatible endpoint로 통합한다.

예:

```bash
vllm serve Qwen/Qwen2.5-Coder-14B-Instruct \
  --host 127.0.0.1 \
  --port 8000 \
  --enable-prefix-caching
```

`ai-common-repo` 설정:

```yaml
models:
  extraction:
    provider: openai_compatible
    base_url: http://127.0.0.1:8000/v1
    api_key: EMPTY
    model: Qwen/Qwen2.5-Coder-14B-Instruct
  summarization:
    provider: openai_compatible
    base_url: http://127.0.0.1:8000/v1
    api_key: EMPTY
    model: Qwen/Qwen2.5-Coder-14B-Instruct
```

vLLM 사용 기준:

- GPU 서버에서 높은 처리량이 필요할 때
- 여러 agent session을 동시에 요약/추출해야 할 때
- OpenAI-compatible endpoint를 중앙 inference service로 운영할 때

---

### 4.6 Ollama Backend

Ollama는 local-first model backend다.

용도:

- privacy-sensitive local extraction
- offline summarization
- small project memory extraction
- embedding backend, 모델 지원 시

예:

```bash
ollama serve
ollama pull qwen2.5-coder:14b
ollama pull nomic-embed-text
```

Ollama native API 또는 OpenAI-compatible API를 사용할 수 있다.

권장: OpenAI-compatible path로 통일한다.

```yaml
models:
  extraction:
    provider: openai_compatible
    base_url: http://127.0.0.1:11434/v1
    api_key: ollama
    model: qwen2.5-coder:14b
  embedding:
    provider: ollama
    base_url: http://127.0.0.1:11434
    model: nomic-embed-text
```

Ollama 사용 기준:

- 외부 API 호출 없이 로컬에서 처리해야 할 때
- 단일 사용자/소규모 작업
- GPU가 없거나 간단한 CPU/로컬 모델로 충분할 때

---

## 5. API 설계

### 5.1 Recall

```http
POST /v1/recall
```

Request:

```json
{
  "query": "현재 작업에 필요한 프로젝트 규칙과 최근 결정사항",
  "user_id": "painmaster",
  "project_id": "repo:fingerprint",
  "tool": "codex",
  "cwd": "/home/ubuntu/agent-work/hermes/project-x",
  "limit": 12,
  "include_profile": true,
  "include_recent": true
}
```

Response:

```json
{
  "profile": {
    "stable": [
      "User prefers verified engineering output."
    ],
    "dynamic": [
      "Recently designing ai-common-repo memory gateway."
    ]
  },
  "memories": [
    {
      "id": "mem_01",
      "type": "project_decision",
      "content": "ClaudeCode and Antigravity-CLI integrate through existing wrapper hooks.",
      "score": 0.91,
      "source": "session_2026_06_25",
      "validity": "active"
    }
  ],
  "context_block": "..."
}
```

---

### 5.2 Ingest Session

```http
POST /v1/sessions/ingest
```

Request:

```json
{
  "schema": "ai-common-repo.session.v1",
  "session_id": "sess_abc",
  "tool": "claudecode",
  "wrapper": true,
  "user_id": "painmaster",
  "project_id": "repo:fingerprint",
  "cwd": "/repo",
  "git": {
    "remote": "git@github.com:org/repo.git",
    "branch": "main",
    "commit_before": "abc123",
    "commit_after": "def456"
  },
  "messages": [
    {
      "role": "user",
      "content": "이 기능 구현해줘"
    },
    {
      "role": "assistant",
      "content": "수정 완료..."
    }
  ],
  "artifacts": {
    "diff_stat": "...",
    "tests": "...",
    "exit_code": 0
  }
}
```

Response:

```json
{
  "accepted": true,
  "session_id": "sess_abc",
  "queued_jobs": [
    "extract_memories",
    "update_profile",
    "index_session"
  ]
}
```

---

### 5.3 Store Explicit Memory

```http
POST /v1/memory
```

```json
{
  "scope": "project",
  "user_id": "painmaster",
  "project_id": "repo:fingerprint",
  "type": "decision",
  "content": "ClaudeCode and Antigravity-CLI must be integrated through existing wrappers, not by modifying vendor binaries.",
  "tags": ["claudecode", "antigravity", "wrapper", "memory"]
}
```

---

### 5.4 Search

```http
POST /v1/search
```

```json
{
  "q": "wrapper 기반 claudecode memory integration",
  "scope": ["user", "project"],
  "project_id": "repo:fingerprint",
  "limit": 20,
  "search_mode": "hybrid"
}
```

---

### 5.5 Profile

```http
GET /v1/profile?user_id=painmaster&project_id=repo:fingerprint
```

---

### 5.6 Model Backend Health

```http
GET /v1/model-backends/health
```

Response:

```json
{
  "backends": [
    {
      "name": "local-vllm",
      "provider": "openai_compatible",
      "base_url": "http://127.0.0.1:8000/v1",
      "status": "ok",
      "models": ["Qwen/Qwen2.5-Coder-14B-Instruct"]
    },
    {
      "name": "local-ollama",
      "provider": "ollama",
      "base_url": "http://127.0.0.1:11434",
      "status": "ok",
      "models": ["qwen2.5-coder:14b", "nomic-embed-text"]
    }
  ]
}
```

---

## 6. Data Model

### 6.1 raw_sessions

```sql
CREATE TABLE raw_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  project_id TEXT,
  tool TEXT NOT NULL,
  wrapper INTEGER DEFAULT 0,
  cwd TEXT,
  git_remote TEXT,
  git_branch TEXT,
  git_commit_before TEXT,
  git_commit_after TEXT,
  started_at TEXT,
  ended_at TEXT,
  exit_code INTEGER,
  raw_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

---

### 6.2 memories

```sql
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  project_id TEXT,
  scope TEXT NOT NULL,
  type TEXT NOT NULL,
  content TEXT NOT NULL,
  normalized_content TEXT,
  source_session_id TEXT,
  confidence REAL DEFAULT 0.8,
  valid_from TEXT,
  valid_until TEXT,
  status TEXT DEFAULT 'active',
  supersedes_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

`scope` values:

- `user`
- `project`
- `repo`
- `global`

`type` values:

- `preference`
- `constraint`
- `decision`
- `workflow`
- `project_fact`
- `warning`
- `credential_hint`
- `handoff`
- `todo_context`

---

### 6.3 documents

```sql
CREATE TABLE documents (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  project_id TEXT,
  source_type TEXT NOT NULL,
  source_uri TEXT,
  title TEXT,
  content TEXT NOT NULL,
  metadata_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

---

### 6.4 document_chunks

```sql
CREATE TABLE document_chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);
```

---

### 6.5 profiles

```sql
CREATE TABLE profiles (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  project_id TEXT,
  profile_type TEXT NOT NULL,
  stable_json TEXT NOT NULL,
  dynamic_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

---

### 6.6 embeddings

```sql
CREATE TABLE embeddings (
  id TEXT PRIMARY KEY,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  model TEXT NOT NULL,
  vector BLOB NOT NULL,
  created_at TEXT NOT NULL
);
```

---

## 7. Memory Extraction Pipeline

```txt
raw session ingest
  │
  ▼
privacy redaction
  │
  ▼
noise filtering
  │
  ▼
summary generation
  │
  ▼
memory candidate extraction
  │
  ▼
type/scope classification
  │
  ▼
dedupe
  │
  ▼
conflict resolver
  │
  ▼
memory write
  │
  ▼
profile rebuild
  │
  ▼
FTS/vector index update
```

Extraction prompt 원칙:

```txt
Extract only durable memories useful in future coding-agent sessions.

Do not extract:
- temporary progress
- exact command output unless reusable
- secrets
- one-time task completion
- unstable file counts or commit hashes
- PR numbers, issue numbers, commit SHAs unless specifically required as durable context

Return JSON array:
[
  {
    "type": "preference|constraint|decision|workflow|project_fact|warning|credential_hint|handoff|todo_context",
    "scope": "user|project|repo|global",
    "content": "...",
    "confidence": 0.0-1.0,
    "reason": "..."
  }
]
```

Model routing:

```txt
if privacy.strict or offline:
  use Ollama backend
elif high throughput queue:
  use vLLM backend
else:
  use configured OpenAI-compatible backend
```

---

## 8. Conflict Resolver

새 memory가 들어오면 기존 active memory와 비교한다.

판정:

- `duplicate`
- `update`
- `contradiction`
- `supersedes`
- `independent`

처리 예:

기존 active memory:

```txt
Project uses direct ClaudeCode binary integration.
```

새 memory:

```txt
ClaudeCode is wrapper-based and should be integrated via wrapper hooks.
```

결과:

```txt
기존 memory.status = superseded
새 memory.status = active
새 memory.supersedes_id = 기존 memory id
profile rebuild queue 등록
```

---

## 9. Project Scope 계산

`project_id` 우선순위:

```txt
1. explicit AI_COMMON_REPO_PROJECT_ID env
2. git remote URL normalized hash
3. git root path hash
4. cwd hash
```

예:

```txt
repo:github.com/org/repo
repo:local:/home/ubuntu/agent-work/hermes/foo
manual:ssafy-security-inspect
```

공통 명령:

```bash
ai-common-repo project-id detect --cwd "$PWD"
```

---

## 10. Context Injection Format

```md
# Persistent Context

## User Stable Preferences
- ...

## Project Active Constraints
- ...

## Recent Relevant Decisions
- ...

## Known Pitfalls
- ...

## Current Task Related Memories
- ...

Rules:
- Use this context as background.
- Do not expose secrets.
- If context conflicts with explicit user instruction, follow the latest user instruction.
- If you discover durable new information, include it in the final summary under "Memory candidates".
```

Context budget:

```yaml
memory:
  max_context_items: 12
  max_context_chars: 12000
  profile_max_chars: 3000
  recent_max_items: 5
```

---

## 11. MCP Server

`ai-common-repo`는 MCP server를 제공해 Codex/ClaudeCode/Antigravity 외 도구도 memory를 사용할 수 있게 한다.

Tools:

- `memory_store`
- `memory_search`
- `memory_recall`
- `memory_profile`
- `memory_forget`
- `session_ingest`

Example MCP config:

```json
{
  "mcpServers": {
    "ai-common-repo": {
      "url": "http://127.0.0.1:28787/mcp"
    }
  }
}
```

---

## 12. CLI 설계

공통 CLI:

```bash
ai-common-repo serve
ai-common-repo status
ai-common-repo recall "query"
ai-common-repo context --tool codex --cwd "$PWD"
ai-common-repo ingest-session session.json
ai-common-repo store --type decision --scope project "..."
ai-common-repo search "..."
ai-common-repo profile
ai-common-repo forget mem_123
ai-common-repo model-backends health
```

Codex:

```bash
codex-memory [codex args...]
codex-memory --no-memory [codex args...]
codex-memory --store "이 프로젝트는 uv를 사용한다"
codex-memory --recall "테스트 실행 규칙"
```

ClaudeCode wrapper hook:

```bash
claudecode-memory pre
claudecode-memory post
```

Antigravity wrapper hook:

```bash
antigravity-memory pre
antigravity-memory post
```

---

## 13. Configuration

Path:

```txt
~/.config/ai-common-repo/config.yaml
```

Example:

```yaml
server:
  host: 127.0.0.1
  port: 28787

storage:
  path: ~/.local/share/ai-common-repo/memory.db
  raw_log_dir: ~/.local/share/ai-common-repo/sessions
  vector_backend: sqlite-vec

identity:
  default_user_id: painmaster

models:
  default_backend: local-ollama
  backends:
    local-ollama:
      provider: openai_compatible
      base_url: http://127.0.0.1:11434/v1
      api_key: ollama
      chat_model: qwen2.5-coder:14b
      embedding_provider: ollama
      embedding_model: nomic-embed-text
    local-vllm:
      provider: openai_compatible
      base_url: http://127.0.0.1:8000/v1
      api_key: EMPTY
      chat_model: Qwen/Qwen2.5-Coder-14B-Instruct
  tasks:
    extraction: local-ollama
    summarization: local-ollama
    conflict_resolution: local-ollama
    reranking: local-vllm

adapters:
  codex:
    enabled: true
    inject_mode: prompt-prefix
  claudecode:
    enabled: true
    integration: wrapper-hooks
  antigravity-cli:
    enabled: true
    integration: wrapper-hooks

memory:
  max_context_items: 12
  max_context_chars: 12000
  enable_conflict_resolution: true
  auto_extract: true
  require_explicit_for_user_profile: false

privacy:
  redact_secrets: true
  raw_session_retention_days: 14
  store_raw_prompts: true
  store_raw_outputs: false
  deny_paths:
    - .env
    - .env.*
    - id_rsa
    - "*.pem"
  secret_patterns:
    - "sk-[A-Za-z0-9_-]+"
    - "ghp_[A-Za-z0-9_]+"
    - "sm_[A-Za-z0-9_]+"
```

---

## 14. Security and Privacy

필수 기능:

1. 저장 전 secret redaction
2. raw log retention policy
3. external model 호출 전 민감정보 제거
4. local-only mode
5. per-command `--no-memory`
6. project-level opt-out
7. sensitive path denylist
8. audit log

권장 policy:

```yaml
privacy:
  mode: local-first
  external_model_requires_redaction: true
  store_raw_outputs: false
  raw_session_retention_days: 14
  extracted_memory_retention_days: null
```

민감정보는 content 자체를 저장하지 않고 hint만 저장한다.

나쁜 예:

```txt
GitHub token is ghp_xxx.
```

좋은 예:

```txt
Project requires a GitHub token from the configured environment file; never store the token value.
```

---

## 15. 구현 단계

### Phase 1: Minimal Local Memory

목표: Codex/ClaudeCode/Antigravity가 로컬 SQLite memory를 공유.

구현:

- SQLite schema
- FTS5 search
- `ai-common-repo serve`
- `/v1/recall`
- `/v1/memory`
- `/v1/sessions/ingest`
- Codex prompt-prefix adapter
- ClaudeCode wrapper pre/post hook
- Antigravity wrapper pre/post hook
- explicit memory store/search

---

### Phase 2: Model Backend Integration

목표: vLLM/Ollama를 extraction/summarization backend로 사용.

구현:

- OpenAI-compatible client
- Ollama native embedding client
- backend health check
- task별 model routing
- timeout/retry/fallback

---

### Phase 3: Automatic Extraction

구현:

- session summarization
- candidate extraction
- type/scope classification
- secret redaction
- durable memory filter
- profile rebuild

---

### Phase 4: Hybrid Search

구현:

- sqlite-vec 또는 LanceDB
- embedding cache
- FTS + vector score fusion
- project/user scope filter
- context block ranking

---

### Phase 5: Conflict Resolution

구현:

- duplicate/update/contradiction/supersedes 판정
- memory validity interval
- profile rebuild queue
- audit log

---

### Phase 6: MCP Server

구현:

- Streamable HTTP MCP endpoint
- memory tools
- project/profile resources
- context prompt

---

## 16. Wrapper별 최종 주의사항

### Codex

- Codex CLI option 변경 가능성에 대비해 adapter를 얇게 유지한다.
- 가능하면 MCP, 아니면 prompt-prefix wrapper.
- session capture는 stdout parsing보다 wrapper envelope를 우선한다.

### ClaudeCode

- 이미 wrapper로 동작 중이므로 wrapper 내부 hook만 추가한다.
- ClaudeCode binary, vendor config, repo `CLAUDE.md`를 직접 수정하지 않는다.
- context는 temp file 또는 wrapper-supported prompt injection으로 전달한다.

### Antigravity-CLI

- 이미 wrapper로 동작 중이므로 double wrapping 금지.
- existing wrapper에 pre/post hook만 붙인다.
- multi-agent metadata를 session envelope에 포함한다.
- 긴 로그는 요약하고 raw log는 retention policy를 적용한다.

### vLLM

- OpenAI-compatible backend로 추상화한다.
- 고처리량 extraction/summarization에 적합하다.
- GPU memory와 concurrency health를 별도로 모니터링한다.

### Ollama

- local-first/privacy-sensitive backend로 사용한다.
- offline extraction/summarization에 적합하다.
- OpenAI-compatible API를 우선 사용하고, embedding은 native Ollama endpoint도 허용한다.

---

## 17. 최종 결론

`ai-common-repo`는 Supermemory의 아이디어인 “AI용 장기기억/컨텍스트 레이어”를 Codex, ClaudeCode, Antigravity-CLI, vLLM, Ollama 환경에 맞게 새로 구현하는 설계다.

핵심 결정:

1. 기존 Supermemory GitHub 코드는 참고하지 않는다.
2. 중앙 `memory-gateway`와 얇은 adapter/hook 구조를 사용한다.
3. ClaudeCode와 Antigravity-CLI는 wrapper 기반 통합만 허용한다.
4. Codex는 MCP 우선, wrapper fallback으로 통합한다.
5. vLLM과 Ollama는 OpenAI-compatible model backend로 추상화한다.
6. 초기 구현은 SQLite + FTS5로 단순하게 시작한다.
7. vector search, conflict resolver, MCP server는 단계적으로 추가한다.
8. 원본 로그보다 durable extracted memory와 profile을 중심 데이터로 둔다.

이 구조는 특정 AI CLI나 특정 memory 제품에 종속되지 않고, 여러 coding agent가 같은 프로젝트 기억과 사용자 선호를 공유하는 실용적인 공용 memory repo가 된다.
