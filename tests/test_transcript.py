"""Transcript recorder tests.

The evaluation reports say what the agent did; the transcript says what the
model was asked and what it replied. Without the second, a divergence between
two runs cannot be explained and a turn cannot be reproduced outside the
harness, so the format is a contract rather than debug output.
"""

from __future__ import annotations

import pytest

from server.transcript import TranscriptLog

LONG_SYSTEM = "SCHEMA AND RULES " * 200


@pytest.fixture
def log(tmp_path):
    return TranscriptLog(tmp_path / "transcript.jsonl")


def record(log, **overrides):
    payload = dict(
        stage="generate", system=LONG_SYSTEM, user="how many farmers?",
        response='{"action":"sql","sql":"SELECT 1"}', model="qwen",
        temperature=0.0, seed=1337, prompt_tokens=3500, completion_tokens=40,
        latency_ms=42000, conversation_id="c1", turn=1,
    )
    payload.update(overrides)
    log.record_call(**payload)


def test_a_call_records_prompt_and_raw_response(log):
    record(log)
    call = log.calls()[0]
    assert call["user"] == "how many farmers?"
    assert call["response"] == '{"action":"sql","sql":"SELECT 1"}'
    assert call["stage"] == "generate"
    assert call["temperature"] == 0.0 and call["seed"] == 1337
    assert call["prompt_tokens"] == 3500


def test_repeated_system_prompts_are_stored_once(log):
    for i in range(5):
        record(log, user=f"question {i}")
    bodies = [r for r in log.read() if r["type"] == "prompt_body"]
    assert len(bodies) == 1, "the identical system prompt should be stored once"
    assert len(log.calls()) == 5
    assert len({c["system_hash"] for c in log.calls()}) == 1


def test_a_different_system_prompt_is_stored_separately(log):
    record(log, stage="generate")
    record(log, stage="narrate", system="You explain results.")
    bodies = [r for r in log.read() if r["type"] == "prompt_body"]
    assert len(bodies) == 2


def test_system_prompt_resolves_back_verbatim(log):
    record(log)
    assert log.resolve(log.calls()[0])["system"] == LONG_SYSTEM


def test_failed_calls_are_recorded_with_the_error(log):
    record(log, response="", error="500 Internal Server Error")
    call = log.calls()[0]
    assert call["error"] == "500 Internal Server Error"
    assert call["response"] == ""


def test_retry_attempts_are_distinguishable(log):
    record(log, attempt=0, temperature=0.0)
    record(log, attempt=1, temperature=0.35)
    record(log, attempt=2, temperature=0.7)
    temps = [(c["attempt"], c["temperature"]) for c in log.calls()]
    assert temps == [(0, 0.0), (1, 0.35), (2, 0.7)]


def test_every_stage_is_attributable_to_a_turn(log):
    for stage in ("classify", "generate", "narrate"):
        record(log, stage=stage, turn=4, conversation_id="c9")
    assert {c["stage"] for c in log.calls()} == {"classify", "generate", "narrate"}
    assert all(c["turn"] == 4 and c["conversation_id"] == "c9" for c in log.calls())


def test_missing_file_reads_as_empty(tmp_path):
    assert TranscriptLog(tmp_path / "none.jsonl").read() == []
