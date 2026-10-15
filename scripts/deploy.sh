#!/usr/bin/env bash
# Deploy ai-common-repo as the compose-managed, Qdrant-bundled container.
#
# Run this from the reviewed web-service/ai-common-repo checkout. It (re)builds
# + (re)starts the compose service from that directory. Idempotent — safe to
# re-run. Data lives on the /data volume (~/.local/share/ai-common-repo), so
# rebuilds preserve memories + Qdrant.
#
# Background: the gateway used to be a host systemd service; since 2026-06-29 it
# runs as a single image that bundles Qdrant (see ../Dockerfile). Before each
# build the running image is tagged ai-common-repo:rollback for quick rollback.
set -euo pipefail

SRC="${ACR_SRC:-/home/ubuntu/web-service/ai-common-repo}"          # compose build context
COMPOSE="${ACR_COMPOSE:-/home/ubuntu/docker-compose.yml}"
SERVICE="${ACR_SERVICE:-ai-common-repo}"
PORT="${ACR_PORT:-28787}"

log() { echo "[deploy $(date '+%H:%M:%S')] $*"; }

[ -f "$SRC/Dockerfile" ] || { echo "no Dockerfile in $SRC" >&2; exit 1; }

# The compose service builds straight from $SRC (the former web-service-docker/
# copy was removed), so there is no sync step.
if sudo -n docker image inspect "$SERVICE:local" >/dev/null 2>&1; then
  sudo -n docker tag "$SERVICE:local" "$SERVICE:rollback"
fi
log "1/2 build + (re)start compose service '$SERVICE'"
sudo -n docker compose -f "$COMPOSE" up -d --build "$SERVICE"

log "2/2 health check :$PORT"
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    log "OK — '$SERVICE' healthy on :$PORT"
    exit 0
  fi
  sleep 1
done

echo "[deploy] FAILED — '$SERVICE' did not become healthy on :$PORT" >&2
sudo -n docker logs "$SERVICE" 2>&1 | tail -20 >&2
echo "[deploy] rollback: sudo docker tag $SERVICE:rollback $SERVICE:local && sudo docker compose -f $COMPOSE up -d --no-build $SERVICE" >&2
exit 1
