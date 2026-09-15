#!/usr/bin/env bash
# Launch llama-server for one model from the bake-off roster.
#
#   ./model/serve.sh qwen2.5-coder-7b
#
# Flags are identical for every candidate so the comparison isolates the model.
# On 8 GB exactly one of these runs at a time.
#
# Note the absence of --n-gpu-layers. Forcing `-ngl 99` on this machine made
# llama.cpp abort its own memory fitting:
#
#   failed to fit params to free device memory: n_gpu_layers already set by
#   user to 99, abort
#
# and generation ran at 3.5 tok/s. Letting llama.cpp choose the split gives
# 5.9 tok/s for the same model -- 65% faster. On a machine with headroom,
# adding -ngl 99 back is the right call; on 8 GB it is actively harmful.
set -euo pipefail

MODEL_ID="${1:-qwen2.5-coder-7b}"
GGUF="model/gguf/${MODEL_ID}.gguf"
PORT="${PORT:-8080}"

if [[ ! -e "$GGUF" ]]; then
  echo "missing $GGUF -- run: uv run python model/download.py --only $MODEL_ID" >&2
  exit 1
fi

# Refuse models that do not fit this machine. Metal shares unified memory with
# the window server, so an OOM here does not just fail the request -- it can
# take the display down. Set ALLOW_UNSAFE_MODEL=1 only on a machine with more
# memory than this one.
if [[ "${ALLOW_UNSAFE_MODEL:-0}" != "1" ]]; then
  PYTHONPATH=. uv run python model/preflight.py "$MODEL_ID" --ctx "${CTX:-4096}" || exit 1
fi

exec llama-server \
  --model "$GGUF" \
  --alias "$MODEL_ID" \
  --host 127.0.0.1 --port "$PORT" \
  --ctx-size "${CTX:-6144}" \
  --parallel 1 \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --jinja \
  --no-warmup
