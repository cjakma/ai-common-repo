# REVIEW — 야간 자율 개발 최종 검토 (Phase 6–9 반영)

> 최초 작성: 2026-06-27 (Phase 5, baseline→Phase 4) · **갱신: 2026-06-30 (Phase 10 야간 — Phase 6–9 반영)**
> 검토 범위: `git diff 6576a7b..HEAD` (baseline → Phase 9, HEAD=`b3e2f26`)
> 결론: **전체 통과**. 신규 코드는 인터페이스 뒤에 캡슐화되고, 비밀 영속 0,
> redaction/SSRF/traversal 가드 모두 제자리, ruff 회귀 0(레거시 baseline 0건으로 정리됨).
> 원본 리포로의 재승격·재배포는 사람 승인 대기.

---

## 0. Phase 6–9 변경 요약 (이번 갱신분)

| Phase | 추가/변경 | 핵심 |
|---|---|---|
| **6 — Qdrant 교정** | `QdrantVectorIndex`(`search/vector.py`), `VECTOR_BACKEND` 기본값 **qdrant**, `qdrant-client>=1.12` 의존, `scripts/backfill_qdrant.py` | **운영자 지시**(local-first보다 우선): 벡터 스토어는 Qdrant. brute-force/sqlite-vec는 오프라인 fallback 전용으로 강등. 테스트는 `QdrantClient(location=":memory:")`로 동일 코드경로 검증. |
| **7 — 잔여 리스크 정리** | `storage/job_repository.py`로 큐 SQL 이전, fallback 벡터 스모크 테스트, 레거시 `E702` **0건 정리**, 커넥터 mock 보강 | REVIEW §5(이전판)의 비차단 후속을 해소: **storage 밖 직접 SQL 0건**, ruff baseline 34→**0**. |
| **8 — RAG 품질 고도화** | `search/rerank.py`(`Reranker` ABC + 결정적 feature scorer + 선택적 cross-encoder), query expansion(camel/snake 분해 + 결정적 동의어), 메모리 dedup job, `scripts/eval_rag.py`(recall@k/MRR) | 신규 런타임 의존 0 — 외부 모델은 전부 선택적, 미설정 시 결정적 fallback으로 오프라인 green. |
| **9 — 관측성/운영성** | `observability/`(metrics/logging/models/reports), `GET /v1/health|metrics|jobs|connectors`, request-id/job-id 상관 로깅, `docs/OPERATIONS.md` | 모든 ops 표면이 redactor 경유(값 아닌 hint만), 읽기 전용·side-effect 0. 스키마 변경 0. |

---

## 1. 검증 결과 (재확인 — 2026-06-30)

| 항목 | 결과 |
|---|---|
| `./.venv/bin/python -m pytest -q` | **94 passed, 1 skipped**, 1 warning (Starlette TestClient httpx deprecation, 무해). skip=sqlite-vec 미설치 fallback 스모크. |
| `./.venv/bin/ruff check .` | **0 errors** (All checks passed). Phase 7에서 레거시 `E702` 34건을 동작 불변으로 정리 — 새 게이트: 신규/수정 파일 clean + 전체 회귀 0. |
| 잔존 프로세스 | **0** (모든 테스트가 인프로세스 TestClient / Fake / Stub / `QdrantClient(":memory:")`, `workers.enabled=false`, 소켓·네트워크 0) |

테스트 분포(95개): workers 11 · search/RAG 13 · MCP 13 · connectors 15 · qdrant 8 ·
rerank 6 · query_expansion 6 · memory_dedup 4 · rag_eval 2 · vector_fallback 6 ·
observability 10 · smoke 1.
핵심 경로(큐 원자성·재시도·idempotency, Qdrant/fallback 벡터 회수·RRF 융합·문서 ingest,
rerank/expansion/dedup, MCP 6툴 E2E·에러코드, 커넥터 폴링·증분·idempotent·토큰 비영속,
health/metrics/jobs/connectors·상관 로깅·비밀 redact)는 모두 커버됨.

---

## 2. 보안 체크리스트

| 점검 | 상태 | 근거 |
|---|---|---|
| **비밀 영속 0** | ✅ | 커넥터 토큰은 `os.environ.get(cfg.token_env)`로 **sync 시점에만** 읽혀 Bearer 헤더로만 사용. DB/로그/커서 미저장 — `connector_state`엔 watermark만. ops API(`/v1/jobs`, `/v1/connectors`)의 last_error·커서는 응답 직전 redact. 로깅 필터가 렌더된 메시지(+`%s` 보간값)를 redactor 경유. `test_connectors`·`test_observability`가 `sk-...` 부재/redact를 검증. |
| **Redaction 위치** | ✅ | 저장 전: `ingest_document`/`_ingest_connector_doc`/`extraction/pipeline`. 외부 모델 호출 전: `external_model_requires_redaction`. 관측 표면(메트릭/로그/상태 API)도 전부 redactor 경유 — 값이 아니라 hint만. |
| **입력 검증 / 크기 상한** | ✅ | HTTP/MCP 모두 pydantic. 문서는 `max_content_chars`(500k)·`max_chunks`(2000)·`chunk_max_chars` 강제. ops 엔드포인트는 GET·무입력(또는 enum `?format=`)이라 표면 최소. |
| **SSRF / 타임아웃** | ✅ | `AllowlistHttpFetcher`: https 강제 + 호스트 allow-list + 타임아웃. 모델 백엔드 `httpx` 타임아웃. **Qdrant 연결처는 config(`AICR_*`/`configs`)에서만** 파생 — 사용자 입력 URL을 그대로 쓰지 않음. |
| **경로 traversal** | ✅ | `_read_allowed_path`: `allowed_roots` 비면 path ingest 비활성, `resolve()` 후 root 밖/비-파일 거부. 기본 `allowed_roots=[]`. |
| **데이터 ≠ 명령** | ✅ | `core/context.py:_RULES` — "silent background only". recall/문서 발췌·rerank 결과가 *배경 데이터*로만 주입. `integrations/README.md`·`docs/OPERATIONS.md`에도 명시. |
| **의존성 최소화** | ✅ | Phase 6에서 **`qdrant-client>=1.12` 1건 추가**(운영자 지시 — local-first보다 우선, §5 기록). Phase 7–9는 신규 런타임 의존 **0**(rerank/expansion/dedup/eval은 stdlib, observability는 stdlib logging + 기존 pydantic/fastapi). 외부 모델(cross-encoder/임베딩)은 전부 선택적. |

---

## 3. 모듈 / 캡슐화 점검

- **인터페이스 뒤 캡슐화** ✅ — 교체 가능한 외부 의존은 모두 ABC/Protocol 뒤:
  `JobQueue`(queue.py), `VectorIndex`(vector.py — `QdrantVectorIndex`/`BruteForceVectorIndex`/
  `SqliteVecVectorIndex`), `Reranker`(rerank.py), `EmbeddingBackend`/`ChatBackend`,
  `Connector`/`HttpFetcher`. 구현체는 factory/registry로 주입(`build_vector_index`가 기본
  qdrant, 미도달 시 설정에 따라 명확한 에러 또는 fallback).
- **레이어 경계** ✅ — HTTP/CLI/MCP는 얇은 shell → `MemoryService`(단일 composition root)
  → repositories/search/extraction/workers/connectors/observability. ops 엔드포인트는 서비스의
  얇은 위임 메서드(`health`/`metrics_report`/`job_status`/`connector_status`)만 호출.
  `observability/reports.py`는 I/O 없는 순수 빌더(probe 결과만 주입받음).
- **SQL 위치** — ✅ **이전판 §5의 예외 해소.** Phase 7에서 `SqliteJobQueue`의 SQL을
  `storage/job_repository.py`로 이전, 큐는 그 위 얇은 래퍼. **storage 밖 직접 SQL 0건**
  (`grep '\.execute(' --exclude-dir=storage` → 0). Phase 9의 readiness/recent_failed/
  connector watermark 조회도 전부 storage 메서드(`Database.ping`/`JobRepository.recent_failed`/
  `ConnectorStateRepository.all`)로 추가.
- **공개 심볼** ✅ — 각 패키지 `__init__.py`가 공개 심볼만 export, 내부 헬퍼는 `_` 접두.

---

## 4. diff 전수 리뷰 (데드코드 / TODO / 임시코드)

- TODO/FIXME/XXX/HACK/temporary/placeholder grep → **실질 적중 0**(적중은 prompt 본문·SQL
  bind "placeholders" 등 무관 문자열).
- 스트레이 `print(` → gateway 0(스크립트 `scripts/*.py`의 CLI 출력은 의도된 stdout).
- 마이그레이션: `SCHEMA_VERSION` 1→4, 모든 신규 테이블 `IF NOT EXISTS`. **Phase 6·8·9는
  스키마 변경 0**(Qdrant는 외부 인덱스, dedup/observability는 순수 추가) → 기존 DB 무중단.
  비밀 컬럼 없음.
- Phase 7 `E702` 정리는 세미콜론 다중문을 줄단위로 분리한 **동작 불변** 리팩터(테스트로 보증).

---

## 5. 잔여 리스크 / 후속 권고

### 해소됨 (이전판 §5 대비)
- ~~**1. `SqliteJobQueue` SQL 위치**~~ → **해소**(Phase 7: `storage/job_repository.py`로 이전).
- ~~**3. 레거시 ruff 34건**~~ → **해소**(Phase 7: `E702` 0건으로 정리, PLAN §1 기준치도 0으로 갱신).

### 잔존 (전부 비차단)
1. **Qdrant 실서버 스모크 미수행 (야간 범위 밖)** — 벡터/하이브리드는 `QdrantClient(":memory:")`로
   전 경로 검증되나, **실 `127.0.0.1:6333` 대상 컬렉션 생성→upsert→query 스모크 1회**는 공유 상태
   변경이라 야간 비진행(PLAN Phase 6 Acceptance의 미체크 항목). 재배포 시 사람이 1회 확인 권장.
2. **sqlite-vec / numpy 미설치** — fallback 경로는 brute-force만 실측(sqlite-vec 스모크는
   `pytest.skip`). 승격 환경에 sqlite-vec 설치 시 한 번 스모크 권장.
3. **커넥터 실연동 미검증** — 실 토큰/네트워크는 야간 정책상 금지라 mock HTTP로만 검증(allow-list
   거부·타임아웃·증분 커서 경계 포함). 승격 후 실 repo/DB로 1회 폴링 스모크 권장.
4. **선택적 외부 모델 미검증** — rerank cross-encoder·임베딩 백엔드·expansion 외부 확장은 *선택적*
   이라 미설정(결정적 fallback만 실측). 활성화 시 백엔드별 스모크 권장.
5. **통합 산출물(`integrations/`)** — 호스트 설정 스키마(특히 OpenClaw)는 빌드별 상이. URL/transport
   (HTTP `/mcp`)를 안정 계약으로 두되 적용은 `APPLY.md` 롤백 절차와 함께 사람이 수동 진행.

---

## 6. 원본 리포 재승격(promote) · 재배포 권고

- 대상: `/home/ubuntu/web-service/ai-common-repo` (현재 read-only 참고용).
- 권고: **사람 승인 후** Phase 6–9 산출물을 재승격. 자동 승격 금지(절대 규칙 5/0). premerge 백업 필수.
- **재배포 필수** — Phase 6에서 `qdrant-client`가 pyproject에 추가되어 이미지 재빌드 없이는 반영
  안 됨. 절차(PLAN Phase 10): 승격 후 `scripts/deploy.sh` → `web-service-docker/ai-common-repo`
  동기화 + `docker compose up -d --build ai-common-repo` + `:28787/healthz` 확인(데이터는 `/data`
  볼륨이라 보존). 실패 시 롤백 가이드는 스크립트가 출력.
- 재승격 전 체크: ① `VECTOR_BACKEND=qdrant` 기본값·연결처 config 확인 ② 위 §5-1 실서버 Qdrant
  스모크 1회 ③ 커넥터 기본 off 유지, 활성화 시 토큰은 `~/.hermes/.env` 등 런타임 env에만 ④ ops
  엔드포인트 노출 범위(로컬 단일 사용자 전제) 확인 ⑤ `integrations/*/APPLY.md`로 호스트별 수동 적용.

---

## 7. 종합

baseline 대비 **워커 토대 · 벡터/하이브리드 검색 + RAG 문서 · MCP 서버(6툴) · GitHub/Notion 폴링
커넥터**(Phase 1–4)에 더해, **Qdrant 벡터 스토어 교정**(Phase 6) · **storage SQL 일원화 + 레거시
ruff 0 정리**(Phase 7) · **rerank/query expansion/dedup/eval RAG 품질 고도화**(Phase 8) ·
**관측성/운영성 표면(health/metrics/jobs/connectors + 상관 로깅 + OPERATIONS.md)**(Phase 9)을
추가했다. **신규 런타임 의존 1건(qdrant-client, 운영자 지시) 외 0, 비밀 영속 0, 회귀 0**, 전체
스위트가 네트워크 없이 green(94 passed/1 skipped, ruff 0). 범위 밖(GDrive/Gmail/OneDrive, 실시간
webhook, multi-modal, 멀티테넌시)은 의도대로 미구현. 잔여 리스크는 모두 비차단.
**원본 재승격·재배포·실서버 Qdrant 스모크는 사람 승인/주간 영역 대기.**
