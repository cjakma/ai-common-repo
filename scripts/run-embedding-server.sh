#!/usr/bin/env bash
# embed-gemma: llama.cpp server with EmbeddingGemma-300M on 172.18.0.1:4176 (pm2).
#
# Only ai-common-repo uses it (OpenAI-compatible /v1/embeddings). It reuses the llama.cpp
# build that llm-broker's runtime/fetch.sh installed; the weights sit next to Gemma 4 E2B:
#   curl -fL -o /home/ubuntu/web-service/llm-broker/runtime/models/embeddinggemma-300M-Q8_0.gguf \
#     https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/main/embeddinggemma-300M-Q8_0.gguf
#   sha256 b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63
#
# Separate from local-llm (:4175) on purpose: --embeddings makes a server embedding-only,
# and sharing local-llm's single slot would queue recall behind long translations.
# Measured on this 4-core aarch64 box with -t 1: ~0.32 s per 280-char memory, RSS ~1.1 GB.
# -t 1: local-llm holds 2 cores; this keeps the 4th core free for guard, NPM and pm2 apps.
# -ub 2048: an embedding input must fit one micro-batch; the 512 default rejects longer texts.
# Host 172.18.0.1 (docker bridge): the ai-common-repo container reaches it there; ufw allows
#   4176 from 172.18.0.0/16 only. Never bind 0.0.0.0.
set -euo pipefail
RUNTIME=/home/ubuntu/web-service/llm-broker/runtime
cd "$RUNTIME"

LLAMA_DIR="$(ls -d bin/llama-* 2>/dev/null | sort -V | tail -n1)"
MODEL="${EMBED_MODEL:-models/embeddinggemma-300M-Q8_0.gguf}"
[[ -x "$LLAMA_DIR/llama-server" && -s "$MODEL" ]] || {
  echo "[embed-gemma] missing llama.cpp or model (see header)" >&2; exit 1; }

export LD_LIBRARY_PATH="$LLAMA_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$LLAMA_DIR/llama-server" \
  -m "$MODEL" --embeddings \
  --host "${EMBED_HOST:-172.18.0.1}" --port "${EMBED_PORT:-4176}" \
  -t "${EMBED_THREADS:-1}" -c "${EMBED_CTX:-2048}" -b 2048 -ub 2048 --no-webui
