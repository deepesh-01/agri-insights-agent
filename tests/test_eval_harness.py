"""Evaluation harness logic: scoring and aggregation.

A bug here does not crash anything — it silently changes every number in the
report. That makes it worth testing more carefully than the code it measures.
"""

from __future__ import annotations

import pytest

from eval.run_eval import EvalRun, TurnScore, score_turn
from server.config import Settings
from server.sql.executor import Executor
from server.sql.validator import validate
from server.catalog import load as load_catalog


class FakeOutcome:
    def __init__(self, action, result=None, answer="", sql=None):
        self.action = action
        self.result = result
        self.answer = answer
        self.sql = sql


@pytest.fixture(scope="module")
def executor():
    ex = Executor(Settings().agent_dsn)
    yield ex
    ex.close()


def make_score(**kwargs) -> TurnScore:
    base = dict(
        conversation="c", turn=1, category="first_turn", question="q",
        expected_action="answer", got_action="answer",
        expected_intent="NEW_TOPIC", got_intent="NEW_TOPIC",
        correct=True, intent_correct=True,
    )
    base.update(kwargs)
    return TurnScore(**base)


# ------------------------------------------------------------------ scoring --
def test_matching_result_sets_score_correct(executor):
    sql = "SELECT count(*) AS n FROM farmer WHERE district = 'Belgaum'"
    turn = {"expect": "answer", "gold_sql": sql}
    correct, _ = score_turn(turn, FakeOutcome("answer", executor.run(sql)), executor)
    assert correct


def test_different_spelling_same_result_scores_correct(executor):
    """The whole point of execution accuracy: text differs, data does not."""
    gold = "SELECT count(*) AS n FROM farmer WHERE district = 'Belgaum'"
    other = ("SELECT count(farmer.id) AS total FROM farmer "
             "WHERE farmer.district = 'Belgaum'")
    turn = {"expect": "answer", "gold_sql": gold}
    correct, _ = score_turn(turn, FakeOutcome("answer", executor.run(other)), executor)
    assert correct


def test_wrong_answer_scores_incorrect(executor):
    gold = "SELECT count(*) AS n FROM farmer WHERE district = 'Belgaum'"
    wrong = "SELECT count(*) AS n FROM farmer WHERE district = 'Mysore'"
    turn = {"expect": "answer", "gold_sql": gold}
    correct, reason = score_turn(turn, FakeOutcome("answer", executor.run(wrong)),
                                 executor)
    assert not correct and "differs" in reason


def test_the_yield_unit_trap_is_actually_caught(executor):
    """The naive query must score wrong, or the harness proves nothing."""
    gold = ("SELECT count(*) AS n FROM crop_cycle c JOIN plot p ON p.id = c.plot_id "
            "WHERE c.actual_yield IS NOT NULL "
            "AND c.actual_yield / p.area_hectares < c.expected_yield")
    naive = ("SELECT count(*) AS n FROM crop_cycle c "
             "WHERE c.actual_yield IS NOT NULL "
             "AND c.actual_yield < c.expected_yield")
    turn = {"expect": "answer", "gold_sql": gold}
    correct, _ = score_turn(turn, FakeOutcome("answer", executor.run(naive)),
                            executor)
    assert not correct


def test_sentinel_trap_is_caught(executor):
    gold = ("SELECT round(avg(value), 4) AS m FROM sensor_reading "
            "WHERE reading_type = 'temperature' "
            "AND value NOT IN (-273, -99, 500, 999.9)")
    naive = ("SELECT round(avg(value), 4) AS m FROM sensor_reading "
             "WHERE reading_type = 'temperature'")
    turn = {"expect": "answer", "gold_sql": gold}
    correct, _ = score_turn(turn, FakeOutcome("answer", executor.run(naive)),
                            executor)
    assert not correct


@pytest.mark.parametrize("expected,got,want", [
    ("refuse", "refuse", True),
    ("refuse", "answer", False),
    ("clarify", "clarify", True),
    ("clarify", "answer", False),
    ("answer", "refuse", False),
])
def test_refusals_and_clarifications_are_scored_outcomes(expected, got, want,
                                                         executor):
    turn = {"expect": expected}
    correct, _ = score_turn(turn, FakeOutcome(got, answer="x"), executor)
    assert correct is want


# -------------------------------------------------------------- aggregation --
def run_with(scores) -> EvalRun:
    return EvalRun(model="m", mode="full", started_at="now",
                   catalog_version=1, prompt_versions={}, scores=scores)


def test_per_turn_accuracy():
    run = run_with([make_score(correct=True), make_score(correct=False),
                    make_score(correct=True), make_score(correct=True)])
    assert run.per_turn_accuracy() == 0.75


def test_full_conversation_accuracy_needs_every_turn():
    run = run_with([
        make_score(conversation="a", correct=True),
        make_score(conversation="a", correct=False),   # one bad turn sinks "a"
        make_score(conversation="b", correct=True),
        make_score(conversation="b", correct=True),
    ])
    assert run.per_turn_accuracy() == 0.75
    assert run.full_conversation_accuracy() == 0.5


def test_intent_accuracy_is_independent_of_sql_accuracy():
    run = run_with([
        make_score(correct=True, intent_correct=False),
        make_score(correct=False, intent_correct=True),
    ])
    assert run.per_turn_accuracy() == 0.5
    assert run.intent_accuracy() == 0.5


def test_category_breakdown():
    run = run_with([
        make_score(category="first_turn", correct=True),
        make_score(category="first_turn", correct=False),
        make_score(category="unanswerable", correct=True),
    ])
    breakdown = run.by_category()
    assert breakdown["first_turn"] == {"n": 2, "correct": 1, "accuracy": 0.5}
    assert breakdown["unanswerable"]["accuracy"] == 1.0
    assert "correction" not in breakdown          # absent categories omitted


def test_latency_percentiles():
    run = run_with([make_score(latency_ms=ms) for ms in
                    [1000, 2000, 3000, 4000, 100000]])
    latency = run.latencies()
    assert latency["p50"] == 3000
    assert latency["p95"] == 100000               # p95 must expose the tail


def test_token_means():
    run = run_with([make_score(prompt_tokens=100, completion_tokens=20),
                    make_score(prompt_tokens=200, completion_tokens=40)])
    tokens = run.tokens()
    assert tokens["prompt_mean"] == 150.0
    assert tokens["total_mean"] == 180.0


def test_retry_rate_and_failures():
    run = run_with([make_score(attempts=1, correct=True),
                    make_score(attempts=3, correct=False, reason="bad")])
    assert run.summary()["retry_rate"] == 0.5
    assert [f.reason for f in run.failures()] == ["bad"]


def test_empty_run_does_not_divide_by_zero():
    run = run_with([])
    assert run.per_turn_accuracy() == 0.0
    assert run.full_conversation_accuracy() == 0.0
    assert run.latencies() == {"p50": 0, "p95": 0, "mean": 0}


def test_summary_is_complete():
    summary = run_with([make_score()]).summary()
    for key in ("model", "mode", "per_turn_accuracy", "full_conversation_accuracy",
                "intent_accuracy", "by_category", "latency_ms",
                "tokens_per_turn", "retry_rate", "catalog_version"):
        assert key in summary


# --------------------------------------------------- eval set sanity check --
def test_every_gold_query_passes_the_agent_s_own_validator():
    """Gold SQL the validator would reject could never be produced by the agent."""
    import pathlib
    import yaml

    catalog = load_catalog()
    for path in sorted(pathlib.Path("eval/conversations").glob("*.yaml")):
        conversation = yaml.safe_load(path.read_text())
        for index, turn in enumerate(conversation["turns"], start=1):
            if sql := turn.get("gold_sql"):
                validate(sql, catalog)   # raises on rejection
