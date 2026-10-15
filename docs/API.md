# ai-common-repo API 명세

> 기준: 실행 중 게이트웨이의 OpenAPI (`GET /openapi.json`, FastAPI `ai-common-repo` 0.1.0) · 작성 2026-09-24 · 2026-10-15 Phase 6–9 병합 반영(관측성 엔드포인트 추가)
> 원본 스키마: [openapi.json](openapi.json) · 설계: [ai-common-repo_architect.md](ai-common-repo_architect.md)

AI 도구들(Claude Code, Hermes, OpenClaw, OpenCode)이 공유하는 **장기 기억 게이트웨이**(Qdrant 내장)와
하네스 간 작업 큐. 앱 데이터 저장소(RDB)로 쓰지 말 것 — ID 로 되읽는 API 가 없고 중복제거·리댁션·청킹이 걸린다.

## 1. 공통

| 항목 | 값 |
|---|---|
| Base URL | `http://127.0.0.1:28787` (Docker, `127.0.0.1` + `172.18.0.1` 이중 publish) |
| 인증 | **없음** — 네트워크 경계(loopback/도커 브리지)로만 보호. 외부 publish 금지 |
| 형식 | JSON, 검증 오류는 `422 {"detail":[…]}` |
| MCP | `POST /mcp` (JSON-RPC 2.0, streamable HTTP) |

## 2. 엔드포인트 요약

| 그룹 | Method | Path |
|---|---|---|
| 상태 | GET | `/healthz`, `/v1/health`, `/v1/metrics`, `/v1/jobs`, `/v1/connectors`, `/v1/model-backends/health` |
| 기억 | POST | `/v1/memory`, `/v1/recall`, `/v1/search`, `/v1/documents`, `/v1/sessions/ingest`, `/v1/memory/{memory_id}/forget` |
| | GET | `/v1/profile` |
| 하네스 큐 | POST | `/v1/harness/tasks`, `/v1/harness/tasks/claim`, `/v1/harness/tasks/{task_id}/status` |
| | GET | `/v1/harness/tasks`, `/v1/harness/tasks/{task_id}` |
| 이벤트 | GET | `/v1/events`, `/v1/events/{event_id}`, `/v1/webhook-subscriptions` |
| | POST | `/v1/webhook-subscriptions/{subscription_id}/test` |
| MCP | POST | `/mcp` |

## 3. MCP 도구 (`POST /mcp`)

| 도구 | 설명 | 필수 인자 |
|---|---|---|
| `memory_store` | 명시적 장기 기억 저장 (preference/decision/fact…) | content |
| `memory_search` | 기억·문서 청크 검색 (fts/vector/hybrid) | q |
| `memory_recall` | profile + 기억 + 문서 발췌를 합친 컨텍스트 블록 | query |
| `memory_profile` | 사용자/프로젝트 범위의 누적 프로필 | - |
| `memory_forget` | id 로 기억 폐기 표시 | memory_id |
| `session_ingest` | 코딩 세션 대화록을 넣어 기억 추출 | tool |
| `harness_request` | 피어 하네스(claudecode/openclaw/opencode)에 작업 등록, task_id 반환 | executor, prompt |
| `harness_result` | task_id 로 상태·결과 조회 | task_id |

```bash
curl -s -X POST http://127.0.0.1:28787/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"memory_search","arguments":{"q":"blacklist-guard unblock","limit":5}}}'
```

## 4. 엔드포인트 상세

아래 샘플 값은 스키마에서 자동 생성한 자리표시값이다.

## root

### GET `/healthz`

Healthz

```bash
curl -s -X GET 'http://127.0.0.1:28787/healthz'
```

응답 `200`: JSON (스키마 미선언)

### POST `/mcp`

Mcp Endpoint

```bash
curl -s -X POST 'http://127.0.0.1:28787/mcp'
```

응답 `200`: JSON (스키마 미선언)


## recall

### POST `/v1/recall`

Recall

요청 본문 (JSON)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `query` | string | O |  |  |
| `user_id` | string \| null | - |  |  |
| `project_id` | string \| null | - |  |  |
| `tool` | string \| null | - |  |  |
| `cwd` | string \| null | - |  |  |
| `limit` | integer \| null | - |  |  |
| `include_profile` | boolean | - | true |  |
| `include_recent` | boolean | - | true |  |

```bash
curl -s -X POST 'http://127.0.0.1:28787/v1/recall' \
  -H 'Content-Type: application/json' \
  -d '{"query": "검색어", "user_id": "ubuntu", "project_id": "web-service", "tool": "claude-code", "cwd": "/home/ubuntu/web-service", "limit": 10, "include_profile": true, "include_recent": true}'
```

응답 `200` 예

```json
{
  "profile": {},
  "memories": [
    {}
  ],
  "context_block": "context_block"
}
```

기타 응답: `422` Validation Error


## memory

### POST `/v1/memory`

Store Memory

요청 본문 (JSON)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `scope` | string | - | "project" |  |
| `user_id` | string \| null | - |  |  |
| `project_id` | string \| null | - |  |  |
| `type` | string | - | "decision" |  |
| `content` | string | O |  |  |
| `tags` | string[] | - |  |  |
| `confidence` | number | - | 0.9 |  |

```bash
curl -s -X POST 'http://127.0.0.1:28787/v1/memory' \
  -H 'Content-Type: application/json' \
  -d '{"scope": "project", "user_id": "ubuntu", "project_id": "web-service", "type": "decision", "content": "본문", "tags": ["tags"], "confidence": 0.9}'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error

### POST `/v1/memory/{memory_id}/forget`

Forget

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `memory_id` | path | string | O |  |  |

```bash
curl -s -X POST 'http://127.0.0.1:28787/v1/memory/mem_01/forget'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error


## sessions

### POST `/v1/sessions/ingest`

Ingest

요청 본문 (JSON)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `schema` | string | - | "ai-common-repo.session.v1" |  |
| `session_id` | string \| null | - |  |  |
| `tool` | string | O |  |  |
| `wrapper` | boolean | - | false |  |
| `user_id` | string \| null | - |  |  |
| `project_id` | string \| null | - |  |  |
| `cwd` | string \| null | - |  |  |
| `git` | object \| null | - |  |  |
| `messages` | object[] | - |  |  |
| `artifacts` | object | - |  |  |

```bash
curl -s -X POST 'http://127.0.0.1:28787/v1/sessions/ingest' \
  -H 'Content-Type: application/json' \
  -d '{"schema": "ai-common-repo.session.v1", "session_id": "sess-20260924-01", "tool": "claude-code", "wrapper": false, "user_id": "ubuntu", "project_id": "web-service", "cwd": "/home/ubuntu/web-service", "git": {"remote": "remote", "branch": "branch", "commit_before": "commit_before", "commit_after": "commit_after"}, "messages": [{"role": "system", "content": "본문"}], "artifacts": {}}'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error


## search

### POST `/v1/search`

Search

요청 본문 (JSON)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `q` | string | O |  |  |
| `scope` | string[] \| null | - |  |  |
| `user_id` | string \| null | - |  |  |
| `project_id` | string \| null | - |  |  |
| `limit` | integer | - | 20 |  |
| `search_mode` | enum(fts, vector, hybrid) | - | "fts" |  |
| `include_documents` | boolean \| null | - |  |  |
| `tags` | string[] \| null | - |  |  |

```bash
curl -s -X POST 'http://127.0.0.1:28787/v1/search' \
  -H 'Content-Type: application/json' \
  -d '{"q": "검색어", "scope": ["scope"], "user_id": "ubuntu", "project_id": "web-service", "limit": 20, "search_mode": "fts", "include_documents": true, "tags": ["tags"]}'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error


## documents

### POST `/v1/documents`

Ingest Document

요청 본문 (JSON)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `user_id` | string \| null | - |  |  |
| `project_id` | string \| null | - |  |  |
| `source_type` | string | - | "text" |  |
| `source_uri` | string \| null | - |  |  |
| `title` | string \| null | - |  |  |
| `content` | string \| null | - |  |  |
| `path` | string \| null | - |  |  |
| `metadata` | object | - |  |  |

```bash
curl -s -X POST 'http://127.0.0.1:28787/v1/documents' \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "ubuntu", "project_id": "web-service", "source_type": "text", "source_uri": "source_uri", "title": "제목", "content": "본문", "path": "path", "metadata": {}}'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error


## profile

### GET `/v1/profile`

Profile

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `user_id` | query | string \| null | - |  |  |
| `project_id` | query | string \| null | - |  |  |

```bash
curl -s -X GET 'http://127.0.0.1:28787/v1/profile?user_id=ubuntu&project_id=web-service'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error


## model-backends

### GET `/v1/model-backends/health`

Backend Health

```bash
curl -s -X GET 'http://127.0.0.1:28787/v1/model-backends/health'
```

응답 `200`: JSON (스키마 미선언)


## harness

### POST `/v1/harness/tasks`

Harness Create

요청 본문 (JSON)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `requester` | string | O |  |  |
| `executor` | string | O |  |  |
| `prompt` | string | O |  |  |
| `priority` | integer | - | 0 |  |
| `task_slug` | string \| null | - |  |  |
| `cwd` | string \| null | - |  |  |
| `permission_profile` | string | - | "accept-edits" |  |
| `review` | string \| null | - |  |  |
| `project_id` | string \| null | - |  |  |
| `user_id` | string \| null | - |  |  |
| `timeout_s` | integer \| null | - |  |  |

```bash
curl -s -X POST 'http://127.0.0.1:28787/v1/harness/tasks' \
  -H 'Content-Type: application/json' \
  -d '{"requester": "hermes", "executor": "executor", "prompt": "prompt", "priority": 0, "task_slug": "task_slug", "cwd": "/home/ubuntu/web-service", "permission_profile": "accept-edits", "review": "review", "project_id": "web-service", "user_id": "ubuntu", "timeout_s": 10}'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error

### GET `/v1/harness/tasks`

Harness List

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `status` | query | string \| null | - |  |  |
| `executor` | query | string \| null | - |  |  |
| `limit` | query | integer | - | 100 |  |

```bash
curl -s -X GET 'http://127.0.0.1:28787/v1/harness/tasks?status=done&executor=executor'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error

### GET `/v1/harness/tasks/{task_id}`

Harness Get

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `task_id` | path | string | O |  |  |

```bash
curl -s -X GET 'http://127.0.0.1:28787/v1/harness/tasks/task_01'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error

### POST `/v1/harness/tasks/claim`

Harness Claim

요청 본문 (JSON)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `executor` | string | O |  |  |
| `worker_id` | string | O |  |  |

```bash
curl -s -X POST 'http://127.0.0.1:28787/v1/harness/tasks/claim' \
  -H 'Content-Type: application/json' \
  -d '{"executor": "executor", "worker_id": "worker_id"}'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error

### POST `/v1/harness/tasks/{task_id}/status`

Harness Status

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `task_id` | path | string | O |  |  |

요청 본문 (JSON)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `status` | enum(running, done, failed, cancelled) | O |  |  |
| `worker_id` | string \| null | - |  |  |
| `result` | object \| null | - |  |  |
| `error` | string \| null | - |  |  |

```bash
curl -s -X POST 'http://127.0.0.1:28787/v1/harness/tasks/task_01/status' \
  -H 'Content-Type: application/json' \
  -d '{"status": "running", "worker_id": "worker_id", "result": {}, "error": "error"}'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error


## events

### GET `/v1/events`

Events List

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `status` | query | string \| null | - |  |  |
| `event_type` | query | string \| null | - |  |  |
| `aggregate_id` | query | string \| null | - |  |  |
| `limit` | query | integer | - | 100 |  |

```bash
curl -s -X GET 'http://127.0.0.1:28787/v1/events?status=done&event_type=event_type&aggregate_id=aggregate_id'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error

### GET `/v1/events/{event_id}`

Events Get

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `event_id` | path | string | O |  |  |

```bash
curl -s -X GET 'http://127.0.0.1:28787/v1/events/evt_01'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error


## webhook-subscriptions

### GET `/v1/webhook-subscriptions`

Webhook Subscriptions

```bash
curl -s -X GET 'http://127.0.0.1:28787/v1/webhook-subscriptions'
```

응답 `200`: JSON (스키마 미선언)

### POST `/v1/webhook-subscriptions/{subscription_id}/test`

Webhook Subscription Test

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `subscription_id` | path | string | O |  |  |

```bash
curl -s -X POST 'http://127.0.0.1:28787/v1/webhook-subscriptions/hermes-inbox/test'
```

응답 `200`: JSON (스키마 미선언)

기타 응답: `422` Validation Error

