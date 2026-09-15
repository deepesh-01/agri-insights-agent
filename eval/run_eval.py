"""Run the evaluation harness. One command, deterministic, same numbers twice.

    uv run python eval/run_eval.py --model qwen2.5-coder-7b
    uv run python eval/run_eval.py --baseline     # zero-shot, no engineering

Determinism comes from temperature 0, a fixed seed, a pinned catalog version
and pinned prompt versions, all of which are recorded in the report header. The
brief asks for the numbers to be reproducible, not for them to be good.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any

import yaml

from eval.compare import compare, gold_is_ordered
from server.audit import AuditLog
from server.catalog import load as load_catalog
from server.config import settings
from server.context.prompts import versions as prompt_versions
from server.conversation.state import ConversationStore
from server.model.client import LlamaCppClient
from server.pipeline import Pipeline
from server.sql.executor import Executor, ExecutionError
from server.transcript import TranscriptLog

CONV_DIR = pathlib.Path("eval/conversations")
REPORT_DIR = pathlib.Path("eval/reports")

CATEGORIES = ["first_turn", "follow_up", "correction", "ambiguous", "unanswerable"]


@dataclass
class TurnScore:
    conversation: str
    turn: int
    category: str
    question: str
    expected_action: str
    got_action: str
    expected_intent: str
    got_intent: str
    correct: bool
    intent_correct: bool
    reason: str = ""
    gold_sql: str | None = None
    got_sql: str | None = None
    attempts: int = 1
    # Every rejected SQL with the validator or database error that rejected it.
    # Without this a failed turn shows only "could not produce a working query",
    # which is not enough to root-cause anything.
    attempt_detail: list[dict] = field(default_factory=list)
    answer: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class EvalRun:
    model: str
    mode: str
    started_at: str
    catalog_version: int
    prompt_versions: dict[str, int]
    scores: list[TurnScore] = field(default_factory=list)

    # ------------------------------------------------------------- aggregates --
    def per_turn_accuracy(self) -> float:
        return _ratio(sum(s.correct for s in self.scores), len(self.scores))

    def intent_accuracy(self) -> float:
        scored = [s for s in self.scores if s.expected_intent]
        return _ratio(sum(s.intent_correct for s in scored), len(scored))

    def full_conversation_accuracy(self) -> float:
        by_conversation: dict[str, list[bool]] = {}
        for score in self.scores:
            by_conversation.setdefault(score.conversation, []).append(score.correct)
        if not by_conversation:
            return 0.0
        perfect = sum(1 for turns in by_conversation.values() if all(turns))
        return _ratio(perfect, len(by_conversation))

    def by_category(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for category in CATEGORIES:
            rows = [s for s in self.scores if s.category == category]
            if rows:
                out[category] = {
                    "n": len(rows),
                    "correct": sum(s.correct for s in rows),
                    "accuracy": _ratio(sum(s.correct for s in rows), len(rows)),
                }
        return out

    def latencies(self) -> dict[str, float]:
        values = sorted(s.latency_ms for s in self.scores)
        if not values:
            return {"p50": 0, "p95": 0, "mean": 0}
        return {
            "p50": _percentile(values, 0.50),
            "p95": _percentile(values, 0.95),
            "mean": round(statistics.mean(values), 1),
        }

    def tokens(self) -> dict[str, float]:
        prompt = [s.prompt_tokens for s in self.scores]
        completion = [s.completion_tokens for s in self.scores]
        if not prompt:
            return {}
        return {
            "prompt_mean": round(statistics.mean(prompt), 1),
            "completion_mean": round(statistics.mean(completion), 1),
            "total_mean": round(statistics.mean(
                [p + c for p, c in zip(prompt, completion)]), 1),
        }

    def failures(self) -> list[TurnScore]:
        return [s for s in self.scores if not s.correct]

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "mode": self.mode,
            "started_at": self.started_at,
            "catalog_version": self.catalog_version,
            "prompt_versions": self.prompt_versions,
            "turns": len(self.scores),
            "conversations": len({s.conversation for s in self.scores}),
            "per_turn_accuracy": self.per_turn_accuracy(),
            "full_conversation_accuracy": self.full_conversation_accuracy(),
            "intent_accuracy": self.intent_accuracy(),
            "by_category": self.by_category(),
            "latency_ms": self.latencies(),
            "tokens_per_turn": self.tokens(),
            "retry_rate": _ratio(
                sum(1 for s in self.scores if s.attempts > 1), len(self.scores)
            ),
        }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _percentile(sorted_values: list[int], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(int(round(fraction * (len(sorted_values) - 1))), len(sorted_values) - 1)
    return float(sorted_values[index])


# ------------------------------------------------------------------- scoring --
def score_turn(
    turn: dict[str, Any],
    outcome,
    executor: Executor,
) -> tuple[bool, str]:
    """Is this turn correct? Refusals and clarifications are scored outcomes."""
    expected = turn["expect"]
    got = outcome.action

    if expected in ("refuse", "clarify"):
        if got == expected:
            return True, f"correctly {expected}d"
        return False, f"expected {expected}, got {got}"

    if got != "answer":
        return False, f"expected an answer, got {got}: {outcome.answer[:120]}"

    gold_sql = turn["gold_sql"]
    try:
        gold = executor.run(gold_sql)
    except ExecutionError as exc:
        return False, f"gold SQL failed to run ({exc.code}) - fix the eval set"

    if outcome.result is None:
        return False, "no result set returned"

    verdict = compare(
        gold.rows, outcome.result.rows, ordered=gold_is_ordered(gold_sql)
    )
    return bool(verdict), verdict.reason


# ---------------------------------------------------------------------- run --
def build_pipeline(model_id: str, *, baseline: bool,
                   run_label: str = "full") -> tuple[Pipeline, Executor]:
    """Construct the agent. `baseline` strips the engineering to zero-shot.

    The baseline is the brief's requested comparison: the same model, the same
    database, no catalog rules, no few-shot examples, no conversation state and
    no repair loop. The delta between it and the full system is what the
    engineering is worth.
    """
    catalog = load_catalog()
    # Every exchange is recorded: without it a report shows what the agent did
    # but not what the model was asked or what it literally replied, which is
    # what any real diagnosis needs.
    transcript = TranscriptLog(
        REPORT_DIR / f"{model_id}-{run_label}.transcript.jsonl"
    )
    model = LlamaCppClient(settings.llama_url, model_id=model_id,
                           transcript=transcript)
    executor = Executor(settings.agent_dsn)
    store = ConversationStore(
        turn_window=settings.turn_window,
        slot_max_age_turns=settings.slot_max_age_turns,
    )
    tuned = settings
    if baseline:
        from dataclasses import replace
        tuned = replace(settings, max_repair_attempts=0, turn_window=0)
        store = ConversationStore(turn_window=0, slot_max_age_turns=0)

    pipeline = Pipeline(
        catalog=catalog, model=model, executor=executor,
        store=store, settings=tuned,
        audit=AuditLog(str(REPORT_DIR / f"{model_id}-{run_label}.audit.jsonl")),
    )
    if baseline:
        _strip_to_baseline(pipeline)
    return pipeline, executor


def _strip_to_baseline(pipeline: Pipeline) -> None:
    """Reduce the system to naive zero-shot prompting of the same model."""
    catalog = pipeline.catalog

    def bare_system_prompt() -> str:
        return (
            "You are a text-to-SQL assistant for a PostgreSQL database.\n"
            "Return JSON: {\"action\":\"sql\",\"sql\":\"...\"}\n\n"
            "Tables:\n" + catalog.render_schema_minimal()
        )

    def bare_context(state, message, policy) -> str:
        return message

    def always_new_topic(state, message, timings):
        timings["classify"] = 0
        return "NEW_TOPIC", None

    pipeline._system_prompt = bare_system_prompt      # noqa: SLF001
    pipeline._context = bare_context                  # noqa: SLF001
    pipeline._classify = always_new_topic             # noqa: SLF001


def run(model_id: str, *, baseline: bool, limit: int | None,
        mode: str | None = None) -> EvalRun:
    label = mode or ("baseline" if baseline else "full")
    # A fresh transcript per run; appending across runs would make the two
    # sides of a determinism comparison impossible to separate.
    for suffix in ("transcript.jsonl", "audit.jsonl"):
        stale = REPORT_DIR / f"{model_id}-{label}.{suffix}"
        stale.unlink(missing_ok=True)
    pipeline, executor = build_pipeline(model_id, baseline=baseline,
                                        run_label=label)
    catalog = pipeline.catalog

    run_record = EvalRun(
        model=model_id,
        mode=mode or ("baseline" if baseline else "full"),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        catalog_version=catalog.version,
        prompt_versions=prompt_versions(),
    )

    paths = sorted(CONV_DIR.glob("*.yaml"))[:limit]
    for path in paths:
        conversation = yaml.safe_load(path.read_text())
        conversation_id = f"eval-{conversation['id']}"
        print(f"\n{conversation['id']}")

        for index, turn in enumerate(conversation["turns"], start=1):
            started = time.perf_counter()
            outcome = pipeline.ask(turn["user"], conversation_id)
            latency = int((time.perf_counter() - started) * 1000)

            correct, reason = score_turn(turn, outcome, executor)
            expected_intent = turn.get("intent", "")
            run_record.scores.append(TurnScore(
                conversation=conversation["id"],
                turn=index,
                category=turn["category"],
                question=turn["user"],
                expected_action=turn["expect"],
                got_action=outcome.action,
                expected_intent=expected_intent,
                got_intent=outcome.intent,
                correct=correct,
                intent_correct=outcome.intent == expected_intent,
                reason=reason,
                gold_sql=turn.get("gold_sql"),
                got_sql=outcome.sql,
                attempts=len(outcome.attempts) or 1,
                attempt_detail=[a.__dict__ for a in outcome.attempts],
                answer=outcome.answer,
                latency_ms=latency,
                prompt_tokens=outcome.tokens.get("prompt", 0),
                completion_tokens=outcome.tokens.get("completion", 0),
            ))
            mark = "PASS" if correct else "FAIL"
            print(f"  turn {index} [{turn['category']:12}] {mark}  "
                  f"{latency/1000:5.1f}s  {reason[:70]}")

    executor.close()
    pipeline.model.close()
    return run_record


# ------------------------------------------------------------------- report --
def write_report(run_record: EvalRun) -> pathlib.Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{run_record.model}-{run_record.mode}"
    json_path = REPORT_DIR / f"{stem}.json"
    json_path.write_text(json.dumps(
        {"summary": run_record.summary(),
         "turns": [asdict(s) for s in run_record.scores]},
        indent=2,
    ), encoding="utf-8")
    return json_path


def print_summary(run_record: EvalRun) -> None:
    summary = run_record.summary()
    print("\n" + "=" * 68)
    print(f"{summary['model']}  ({summary['mode']})")
    print("=" * 68)
    print(f"  conversations            {summary['conversations']}")
    print(f"  turns                    {summary['turns']}")
    print(f"  per-turn accuracy        {summary['per_turn_accuracy']:.1%}")
    print(f"  full-conversation acc.   {summary['full_conversation_accuracy']:.1%}")
    print(f"  intent accuracy          {summary['intent_accuracy']:.1%}")
    print(f"  retry rate               {summary['retry_rate']:.1%}")
    print(f"  latency p50 / p95        {summary['latency_ms']['p50']/1000:.1f}s / "
          f"{summary['latency_ms']['p95']/1000:.1f}s")
    tokens = summary["tokens_per_turn"]
    if tokens:
        print(f"  tokens per turn          {tokens['prompt_mean']:.0f} prompt + "
              f"{tokens['completion_mean']:.0f} completion")
    print("\n  by category:")
    for category, stats in summary["by_category"].items():
        print(f"    {category:14} {stats['correct']:>3}/{stats['n']:<3} "
              f"{stats['accuracy']:.1%}")

    failures = run_record.failures()
    if failures:
        print(f"\n  failures ({len(failures)}):")
        for failure in failures:
            print(f"    {failure.conversation} turn {failure.turn} "
                  f"[{failure.category}]: {failure.reason[:80]}")
            for attempt in failure.attempt_detail:
                if attempt.get("error"):
                    print(f"        {attempt['verdict']} "
                          f"({attempt.get('error_code')}): "
                          f"{attempt['error'][:90]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=settings.model_id)
    parser.add_argument("--baseline", action="store_true",
                        help="zero-shot, no catalog, no state, no repair loop")
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N conversations (for smoke runs)")
    args = parser.parse_args()

    run_record = run(args.model, baseline=args.baseline, limit=args.limit)
    print_summary(run_record)
    path = write_report(run_record)
    print(f"\nreport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
