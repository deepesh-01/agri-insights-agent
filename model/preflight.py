"""Refuse to launch a model that will not fit in available memory.

This exists because of a real incident: repeatedly launching 8B GGUF models on
an 8 GB M1 drove Metal into
`kIOGPUCommandBufferCallbackErrorOutOfMemory`, and because unified memory is
shared with the window server, the display began glitching and the machine had
to be shut down. A failed inference experiment took down the whole desktop.

So "does not fit" is decided before anything is loaded, from the file size and
the machine's actual free memory -- not discovered by watching a GPU die.

    uv run python model/preflight.py qwen3-8b        # exits non-zero if unsafe
    uv run python model/preflight.py --all           # report on the roster
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

import yaml

GGUF_DIR = pathlib.Path("model/gguf")
ROSTER = pathlib.Path("model/models.yaml")

# Headroom the rest of the system needs: macOS, the window server, Postgres,
# the agent process and a browser. Below this, Metal starts failing rather than
# swapping gracefully.
#
# Calibrated against what this machine actually did, not from theory. Observed
# on 8 GB: a 4.36 GB model runs; a 4.58 GB model OOMs. That puts the real
# budget between 4.95 and 5.17 GB once KV and compute buffers are counted, so
# 2.9 GB reserved (5.1 GB budget) sits inside the observed boundary at both
# contexts used here: it admits the 7B at ctx 6144 (proven over an 18-turn
# run) and still refuses both 8B models at ctx 4096. An earlier value of 2.6
# predicted the 8B models would fit, and they demonstrably do not; 3.0 went too
# far the other way and blocked a configuration already known to work.
RESERVED_GB = 2.9

# KV cache per 1k context, measured for a 7-8B model with q8_0 K/V.
KV_GB_PER_1K_CTX = 0.035

# Compute buffers scale with batch size; llama.cpp's default batch of 2048 was
# itself enough to tip an 8B model over on this machine.
COMPUTE_OVERHEAD_GB = 0.45


def physical_memory_gb() -> float:
    out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip()) / 1024**3


def usable_gb() -> float:
    """Memory a model may claim: physical minus what the system needs."""
    return physical_memory_gb() - RESERVED_GB


def estimate_gb(gguf: pathlib.Path, ctx: int) -> float:
    weights = gguf.stat().st_size / 1024**3
    return weights + (ctx / 1000) * KV_GB_PER_1K_CTX + COMPUTE_OVERHEAD_GB


def observed_failures() -> dict[str, str]:
    """Models this machine has actually failed to run, and why.

    An estimate can be wrong in the direction that hurts; a recorded
    observation cannot. This list wins over the arithmetic.
    """
    roster = yaml.safe_load(ROSTER.read_text())
    return {
        m["id"]: m["known_incompatible"]
        for m in roster["models"] if m.get("known_incompatible")
    }


def check(model_id: str, ctx: int) -> tuple[bool, str]:
    gguf = GGUF_DIR / f"{model_id}.gguf"
    if not gguf.exists():
        return False, f"{model_id}: not downloaded"

    if reason := observed_failures().get(model_id):
        return False, f"{model_id}: {reason}"

    needed = estimate_gb(gguf, ctx)
    budget = usable_gb()
    margin = budget - needed
    detail = (f"{model_id}: needs ~{needed:.2f} GB at ctx {ctx}, "
              f"budget {budget:.2f} GB, margin {margin:+.2f} GB")
    return margin > 0, detail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_id", nargs="?")
    parser.add_argument("--ctx", type=int, default=4096)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if args.all:
        roster = yaml.safe_load(ROSTER.read_text())
        ctx = roster["defaults"]["ctx"]
        print(f"physical {physical_memory_gb():.1f} GB, "
              f"reserved {RESERVED_GB} GB, budget {usable_gb():.2f} GB, ctx {ctx}\n")
        for model in roster["models"]:
            ok, detail = check(model["id"], ctx)
            print(f"  {'FITS    ' if ok else 'TOO BIG '} {detail}")
        return 0

    if not args.model_id:
        parser.error("give a model id or --all")

    ok, detail = check(args.model_id, args.ctx)
    print(detail, file=sys.stderr if not ok else sys.stdout)
    if not ok:
        print("refusing to launch: this would exhaust unified memory and can "
              "take the display down with it", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
