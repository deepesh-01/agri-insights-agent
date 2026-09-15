#!/usr/bin/env bash
# Block until llama-server is actually ready to serve.
#
#   ./model/wait_healthy.sh [url] [timeout_seconds]
#
# llama-server returns HTTP 503 while it is still loading weights, and
# `curl -s -o /dev/null <url>` exits 0 on a 503 because the request itself
# succeeded. A wait loop written that way breaks out the moment the port opens
# and fires an entire evaluation at a server that has not loaded, which is
# exactly what happened: 54 of 54 calls returned 503 and the run scored 10.9%
# with zero tokens consumed.
#
# The status code is what matters, so it is what gets checked.
set -uo pipefail

URL="${1:-http://127.0.0.1:8080/health}"
TIMEOUT="${2:-300}"
INTERVAL=3
elapsed=0

while (( elapsed < TIMEOUT )); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL" 2>/dev/null || echo "000")
  if [[ "$code" == "200" ]]; then
    echo "healthy after ${elapsed}s"
    exit 0
  fi
  sleep "$INTERVAL"
  elapsed=$(( elapsed + INTERVAL ))
done

echo "not healthy after ${TIMEOUT}s (last status: ${code:-none})" >&2
exit 1
