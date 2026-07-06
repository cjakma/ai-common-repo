# ai-common-repo — self-contained image: the memory gateway + an in-image Qdrant
# vector store. One artifact so the whole shared-memory stack runs from a single
# compose service. Qdrant is bundled per operator directive (RAG store = Qdrant).
#
#   stage 1: pull the pinned Qdrant binary + its config/static out of the official image
#   stage 2: python runtime with the gateway installed; entrypoint runs both

# ── stage 1: Qdrant binary ──────────────────────────────────────────────────
FROM qdrant/qdrant:v1.18.2 AS qdrant

# ── stage 2: gateway runtime (+ bundled qdrant) ─────────────────────────────
FROM python:3.12-slim AS runtime

# tini = clean PID 1 (forwards signals, reaps); curl for healthcheck/readiness;
# libunwind8 = Qdrant binary runtime dep (libunwind-ptrace / arch-specific unwind)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini curl ca-certificates libunwind8 \
    && rm -rf /var/lib/apt/lists/*

# bundle Qdrant: binary + its default config + web UI static assets
COPY --from=qdrant /qdrant/qdrant /usr/local/bin/qdrant
COPY --from=qdrant /qdrant/config /qdrant/config
COPY --from=qdrant /qdrant/static /qdrant/static

# install the gateway (monorepo packages declared in pyproject)
WORKDIR /app
COPY pyproject.toml README.md ./
COPY packages/ packages/
RUN pip install --no-cache-dir -e .

# config-driven (AICR_* / QDRANT__* env overrides). All mutable state on /data so
# a single volume persists both the SQLite memory DB and the Qdrant collections.
ENV AICR_SERVER__HOST=0.0.0.0 \
    AICR_SERVER__PORT=28787 \
    AICR_STORAGE__DB_PATH=/data/memory.db \
    AICR_STORAGE__RAW_LOG_DIR=/data/sessions \
    QDRANT__STORAGE__STORAGE_PATH=/data/qdrant \
    QDRANT__SERVICE__HOST=127.0.0.1
RUN mkdir -p /data/qdrant /data/sessions

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 28787
# tini -g: signal the whole process group so qdrant + gateway stop together
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/entrypoint.sh"]
