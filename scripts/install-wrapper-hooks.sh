#!/usr/bin/env bash
# Reference snippet: how an existing ClaudeCode/Antigravity wrapper calls the hooks.
# This does NOT modify any vendor binary; it only shows the integration points.
set -euo pipefail
echo "Add to your claudecode wrapper:"
cat <<'EOF'
  CTX=$(mktemp)
  claudecode-memory pre --cwd "$PWD" --argv-json "$ARGV_JSON" --out-context "$CTX"
  # inject "$CTX" via your wrapper's supported prompt/system mechanism
  <run real claude>
  claudecode-memory post --cwd "$PWD" --session-id "$SID" --exit-code "$?" \
    --stdout "$STDOUT_LOG" --diff "$DIFF_FILE"
EOF
