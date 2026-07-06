#!/usr/bin/env bash
# Start the bundled Qdrant (loopback-only inside the container), wait until it is
# ready, then start the gateway. The two live and die together: if either exits,
# we tear down the other and exit non-zero so the container's restart policy
# recreates the whole stack.
set -uo pipefail

QDRANT_PID=""
GW_PID=""

shutdown() {
  kill -TERM "$GW_PID" "$QDRANT_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap shutdown TERM INT

# 1) Qdrant — config from /qdrant/config, storage on the /data volume.
mkdir -p /data/qdrant /data/sessions
( cd /qdrant && exec qdrant ) >/tmp/qdrant.log 2>&1 &
QDRANT_PID=$!

for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:6333/readyz >/dev/null 2>&1; then
    echo "[entrypoint] qdrant ready (${i}s)"
    break
  fi
  if ! kill -0 "$QDRANT_PID" 2>/dev/null; then
    echo "[entrypoint] qdrant exited during startup; log:"; cat /tmp/qdrant.log; exit 1
  fi
  sleep 1
done

# 2) Gateway (the user-facing :28787 HTTP/MCP surface).
cd /app
python -m ai_common_repo_cli serve &
GW_PID=$!

# Exit as soon as either process exits, then clean up the survivor.
wait -n "$QDRANT_PID" "$GW_PID"
echo "[entrypoint] a child exited; shutting down the stack"
shutdown
exit 1
