#!/usr/bin/env bash
# Deploy ai-common-repo as the compose-managed, Qdrant-bundled container.
#
# Run this AFTER the nightly autonomous dev has promoted the sandbox into
# web-service/ai-common-repo AND a human has reviewed it. It syncs the promoted
# source into the docker build context and (re)builds + (re)starts the compose
# service. Idempotent — safe to re-run. Data lives on the /data volume
# (~/.local/share/ai-common-repo), so rebuilds preserve memories + Qdrant.
#
# Background: the gateway used to be a host systemd service; since 2026-06-29 it
# runs as a single image that bundles Qdrant (see ../Dockerfile). The old
# systemd unit is disabled but kept for rollback (`sudo systemctl start
# ai-common-repo`).
set -euo pipefail

SRC="${ACR_SRC:-/home/ubuntu/web-service/ai-common-repo}"          # promoted source
CTX="${ACR_CTX:-/home/ubuntu/web-service-docker/ai-common-repo}"   # docker build context
COMPOSE="${ACR_COMPOSE:-/home/ubuntu/docker-compose.yml}"
SERVICE="${ACR_SERVICE:-ai-common-repo}"
PORT="${ACR_PORT:-28787}"

log() { echo "[deploy $(date '+%H:%M:%S')] $*"; }

[ -f "$SRC/Dockerfile" ] || { echo "no Dockerfile in $SRC — has the sandbox been promoted/merged?" >&2; exit 1; }

log "1/3 sync promoted source -> build context ($CTX)"
rsync -a --delete \
  --exclude '.venv/' --exclude '.git/' --exclude '.night/' \
  --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
  --exclude '*.egg-info/' --exclude '__pycache__/' \
  "$SRC"/ "$CTX"/

log "2/3 build + (re)start compose service '$SERVICE'"
sudo -n docker compose -f "$COMPOSE" up -d --build "$SERVICE"

log "3/3 health check :$PORT"
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    log "OK — '$SERVICE' healthy on :$PORT"
    exit 0
  fi
  sleep 1
done

echo "[deploy] FAILED — '$SERVICE' did not become healthy on :$PORT" >&2
sudo -n docker logs "$SERVICE" 2>&1 | tail -20 >&2
echo "[deploy] rollback option: sudo systemctl start ai-common-repo  (old systemd unit, if still present)" >&2
exit 1
