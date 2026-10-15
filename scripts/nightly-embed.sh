#!/usr/bin/env bash
# Nightly embedding maintenance for ai-common-repo (systemd aicr-nightly-embed.timer, 03:30).
#
# Before the switch (first night): build the EmbeddingGemma collection while the gateway keeps
# serving from the old hash-embedding one (~2.3 memories/s on one core), then route the
# embedding task to embed-gemma + point storage at the new collection, restart, embed whatever
# was stored meanwhile, and verify. Any failure after the config change restores the backup.
# After the switch (every later night): embed memories stored while embed-gemma was down
# (stores are fail-soft) and prune points of forgotten memories.
#
# Re-embedding runs at night because embed-gemma has one thread and a full pass keeps that
# core busy for the whole run. The old collection is kept for rollback.
set -Eeuo pipefail   # -E: the rollback ERR trap also fires inside functions

SRC=/home/ubuntu/web-service/ai-common-repo
CFG=/home/ubuntu/.local/share/ai-common-repo/config.yaml
CONTAINER=ai-common-repo
BACKEND=embed-gemma
TARGET="${AICR_EMBED_COLLECTION:-ai_common_repo_embgemma768}"
GATEWAY=http://127.0.0.1:28787
EMBED_HEALTH=http://172.18.0.1:4176/health

log() { echo "[nightly-embed $(date '+%F %T')] $*"; }
reembed() { sudo -n docker exec -i -w /app "$CONTAINER" python - "$@" < "$SRC/scripts/reembed.py"; }
in_container() { sudo -n docker exec -i -w /app "$CONTAINER" python - "$@"; }
wait_healthy() {
  for _ in $(seq 1 60); do curl -sf -m2 "$GATEWAY/healthz" >/dev/null && return 0; sleep 1; done
  return 1
}

curl -sf -m5 "$EMBED_HEALTH" >/dev/null || { log "embed-gemma is not healthy — skipping"; exit 1; }

switched=$(in_container "$BACKEND" "$TARGET" <<'PY'
import sys
from ai_common_repo.core.config import load_config
c = load_config()
print("yes" if c.models.tasks.get("embedding") == sys.argv[1]
      and c.storage.qdrant_collection == sys.argv[2] else "no")
PY
)

if [[ "$switched" == "yes" ]]; then
  log "already on $BACKEND/$TARGET — catch-up pass"
  reembed --prune
  exit 0
fi

log "1/4 building $TARGET with $BACKEND (gateway keeps serving the old collection)"
reembed --collection "$TARGET" --backend "$BACKEND"

log "2/4 switching config (backup: $CFG.bak-pre-switch)"
cp -p "$CFG" "$CFG.bak-pre-switch"
rollback() {
  trap - ERR
  log "FAILED — restoring $CFG and restarting"
  cp -p "$CFG.bak-pre-switch" "$CFG"
  sudo -n docker restart "$CONTAINER" >/dev/null
  wait_healthy && log "rolled back; gateway healthy on the old collection"
  exit 1
}
trap rollback ERR
in_container "$BACKEND" "$TARGET" /data/config.yaml <<'PY'
import sys, yaml
backend, target, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
cfg.setdefault("models", {}).setdefault("tasks", {})["embedding"] = backend
cfg.setdefault("storage", {})["qdrant_collection"] = target
with open(path, "w") as f:  # rewrite in place: keeps the host file's owner/mode
    yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
PY
sudo -n docker restart "$CONTAINER" >/dev/null
wait_healthy

log "3/4 embedding what was stored during the build"
reembed --prune

log "4/4 verifying"
curl -sf -m30 "$GATEWAY/v1/health" | python3 -c '
import json, sys
h = json.load(sys.stdin)
c = {x["name"]: x for x in h["components"]}
vs, em = c["vector_store"], c["embedding"]
assert h["status"] == "ok", h
assert vs["info"]["collection"] == sys.argv[1] and vs["info"]["vector_size"] == 768, vs
assert em["info"]["backend"] == sys.argv[2], em
print("health ok:", vs["info"]["points"], "points,", em["info"]["model"])
' "$TARGET" "$BACKEND"
curl -sf -m30 -XPOST "$GATEWAY/v1/search" -H 'content-type: application/json' \
  -d '{"q":"배포 규칙","search_mode":"vector","limit":3}' >/dev/null
trap - ERR
log "switched to $BACKEND/$TARGET (old collection ai_common_repo kept for rollback)"
