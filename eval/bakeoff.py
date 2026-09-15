"""Evaluate every model in the roster and write the comparison report.

Only one model fits in 8 GB, so this is strictly serial: start llama-server,
wait for it to load, run the harness, stop it, reclaim the memory, repeat. That
constraint is also why the runtime flags are identical for every candidate --
with one variable (the model) the comparison means something.

    uv run python eval/bakeoff.py                    # every model, full set
    uv run python eval/bakeoff.py --limit 4          # screening run
    uv run python eval/bakeoff.py --only qwen3-8b llama-3.1-8b
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
from typing import Any

import httpx
import yaml

from eval.run_eval import print_summary, run, write_report

ROSTER = pathlib.Path("model/models.yaml")
REPORT_DIR = pathlib.Path("eval/reports")
DOC_PATH = pathlib.Path("docs/MODEL_REPORT.md")
HEALTH_URL = "http://127.0.0.1:8080/health"
LOAD_TIMEOUT_S = 300


def start_server(model_id: str, log_path: pathlib.Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w")
    process = subprocess.Popen(
        ["./model/serve.sh", model_id],
        stdout=handle, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + LOAD_TIMEOUT_S
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{model_id}: llama-server exited; see {log_path}")
        try:
            if httpx.get(HEALTH_URL, timeout=2).status_code == 200:
                return process
        except httpx.HTTPError:
            pass
        time.sleep(2)
    process.terminate()
    raise RuntimeError(f"{model_id}: llama-server did not become healthy")


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    # The weights are several GB of wired memory; give the OS a moment to
    # reclaim them before the next model is loaded.
    time.sleep(3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N conversations (screening run)")
    parser.add_argument("--only", nargs="*", default=None,
                        help="restrict to these model ids")
    parser.add_argument("--baseline-for", default=None,
                        help="also run the zero-shot baseline for this model id")
    args = parser.parse_args()

    results_skipped: list[dict[str, Any]] = []
    roster = yaml.safe_load(ROSTER.read_text())
    models = roster["models"]
    if args.only:
        models = [m for m in models if m["id"] in args.only]

    # Anything this machine has been observed to fail on is skipped outright
    # rather than retried. See model/preflight.py for why.
    available, missing, unsafe = [], [], []
    for model in models:
        path = pathlib.Path("model/gguf") / f"{model['id']}.gguf"
        if not path.exists():
            missing.append(model)
        elif model.get("known_incompatible"):
            unsafe.append(model)
        else:
            available.append(model)
    if missing:
        print("skipping (not downloaded): "
              + ", ".join(m["id"] for m in missing), file=sys.stderr)
    for model in unsafe:
        reason = " ".join(model["known_incompatible"].split())
        print(f"skipping {model['id']}: {reason}", file=sys.stderr)
        results_skipped.append({"model": model["id"], "error": reason})
    if not available:
        print("no models available", file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = list(results_skipped)
    for model in available:
        model_id = model["id"]
        log_path = pathlib.Path(f"eval/reports/{model_id}.server.log")
        print(f"\n{'=' * 68}\n{model_id} -- starting llama-server\n{'=' * 68}")

        started = time.time()
        try:
            process = start_server(model_id, log_path)
        except RuntimeError as exc:
            print(f"  FAILED to start: {exc}", file=sys.stderr)
            results.append({"model": model_id, "error": str(exc)})
            continue
        print(f"  ready in {time.time() - started:.0f}s")

        try:
            # Screening and full runs must not overwrite each other's reports.
            mode = "screen" if args.limit else "full"
            record = run(model_id, baseline=False, limit=args.limit, mode=mode)
            print_summary(record)
            write_report(record)
            summary = record.summary()
            summary["params"] = model.get("params")
            summary["rationale"] = " ".join(model.get("rationale", "").split())
            results.append(summary)

            if args.baseline_for == model_id:
                print(f"\n{model_id} -- zero-shot baseline")
                baseline = run(model_id, baseline=True, limit=args.limit)
                print_summary(baseline)
                write_report(baseline)
                results.append({**baseline.summary(), "params": model.get("params")})
        finally:
            stop_server(process)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = "bakeoff-screen" if args.limit else "bakeoff"
    (REPORT_DIR / f"{stem}.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    write_markdown(results, limit=args.limit)
    print(f"\nwrote {DOC_PATH}")
    return 0


def write_markdown(results: list[dict[str, Any]], *, limit: int | None) -> None:
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    scored = [r for r in results
              if "error" not in r and r.get("mode") in ("full", "screen")]
    scored.sort(key=lambda r: r["per_turn_accuracy"], reverse=True)

    lines = [
        "# Model bake-off",
        "",
        "Generated by `eval/bakeoff.py`. Every candidate is <=8B parameters, "
        "Q4_K_M, served by `llama-server` with identical flags "
        "(`-c 8192 -np 1 -ngl 99`, q8_0 KV cache) on the same hardware, and "
        "scored on the same evaluation set with the same prompts.",
        "",
        f"Hardware: Apple M1, 8 GB unified memory, macOS. "
        f"Evaluation set: {'first ' + str(limit) + ' conversations' if limit else 'all 16 conversations, 55 turns'}.",
        "",
        "## Results",
        "",
        "| Model | Params | Per-turn | Full-conversation | Intent | p50 | p95 | Retry rate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in scored:
        lines.append(
            f"| {row['model']} | {row.get('params', '?')} | "
            f"{row['per_turn_accuracy']:.1%} | "
            f"{row['full_conversation_accuracy']:.1%} | "
            f"{row['intent_accuracy']:.1%} | "
            f"{row['latency_ms']['p50'] / 1000:.1f}s | "
            f"{row['latency_ms']['p95'] / 1000:.1f}s | "
            f"{row['retry_rate']:.1%} |"
        )

    lines += ["", "## Accuracy by question category", "",
              "| Model | " + " | ".join(
                  c.replace("_", " ") for c in
                  ["first_turn", "follow_up", "correction", "ambiguous", "unanswerable"]
              ) + " |",
              "|---|" + "---|" * 5]
    for row in scored:
        cells = []
        for category in ["first_turn", "follow_up", "correction",
                         "ambiguous", "unanswerable"]:
            stats = row.get("by_category", {}).get(category)
            cells.append(f"{stats['correct']}/{stats['n']}" if stats else "-")
        lines.append(f"| {row['model']} | " + " | ".join(cells) + " |")

    failed = [r for r in results if "error" in r]
    if failed:
        lines += ["", "## Did not run", ""]
        lines += [f"- **{r['model']}**: {r['error']}" for r in failed]

    lines += ["", "## Why each model is in the set", ""]
    for row in scored:
        if row.get("rationale"):
            lines.append(f"- **{row['model']}** — {row['rationale']}")

    DOC_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
