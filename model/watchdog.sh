#!/usr/bin/env bash
# Kill the evaluation if the machine starts to struggle.
#
# Metal shares unified memory with the window server on Apple Silicon, so an
# out-of-memory model does not merely fail its request -- it can destabilise the
# display. This watchdog stops the run at the first sign of that rather than
# letting it degrade, because the machine matters more than the datapoint.
#
# The hard signal is a Metal out-of-memory in the server log. That is what
# actually destabilised the display, and it now cannot happen for a model the
# preflight admits.
#
# Swap is only a secondary signal, and the threshold is calibrated on observed
# behaviour: simply loading a 7B Q4_K_M takes swap to ~3.0 GB on this machine,
# and the model ran an entire 18-turn screening pass at ~3.7 GB with no
# trouble. A first attempt at 2.5 GB killed a perfectly healthy run before its
# first turn. 5.0 GB sits well above normal operation while still catching a
# genuine runaway.
#
#   ./model/watchdog.sh <llama-server-log> [swap_limit_mb]
set -uo pipefail

LOG="${1:?usage: watchdog.sh <llama-server-log> [swap_limit_mb]}"
SWAP_LIMIT_MB="${2:-5000}"
INTERVAL=20

stop_everything() {
  echo "WATCHDOG: $1 -- stopping evaluation" >&2
  pkill -f bakeoff.py 2>/dev/null
  pkill -f run_eval.py 2>/dev/null
  pkill -f llama-server 2>/dev/null
  exit 1
}

while true; do
  pgrep -f "bakeoff.py|run_eval.py" >/dev/null || exit 0   # run finished

  if [[ -f "$LOG" ]] && grep -qiE "OutOfMemory|Insufficient Memory|backend is in error state" "$LOG"; then
    stop_everything "Metal out-of-memory in $LOG"
  fi

  swap_mb=$(sysctl -n vm.swapusage | sed -E 's/.*used = ([0-9.]+)M.*/\1/')
  if [[ -n "$swap_mb" ]] && (( ${swap_mb%.*} > SWAP_LIMIT_MB )); then
    stop_everything "swap ${swap_mb}M exceeds ${SWAP_LIMIT_MB}M"
  fi

  sleep "$INTERVAL"
done
