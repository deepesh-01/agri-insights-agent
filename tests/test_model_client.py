"""Model-client tests, focused on parsing what weaker models actually return.

The bake-off includes candidates that do not reliably honour a JSON schema. A
model that writes correct SQL inside a code fence should be scored on the SQL,
not penalised for packaging, so these tolerances are load-bearing for the
fairness of the comparison rather than mere convenience.
"""

from __future__ import annotations

import json

import pytest

from server.model.client import Completion


def completion(text: str) -> Completion:
    return Completion(text=text)


def test_plain_json_parses():
    assert completion('{"action": "sql", "sql": "SELECT 1"}').json()["action"] == "sql"


def test_fenced_json_parses():
    text = '```json\n{"action": "sql", "sql": "SELECT 1"}\n```'
    assert completion(text).json()["sql"] == "SELECT 1"


def test_unlabelled_fence_parses():
    text = '```\n{"action": "refuse"}\n```'
    assert completion(text).json()["action"] == "refuse"


def test_prose_around_json_is_stripped():
    text = 'Here is the query you asked for:\n{"action": "sql", "sql": "SELECT 1"}\nHope that helps.'
    assert completion(text).json()["sql"] == "SELECT 1"


def test_leading_and_trailing_whitespace():
    assert completion('\n\n  {"action": "sql"}  \n').json()["action"] == "sql"


def test_genuinely_unparseable_output_raises():
    with pytest.raises(json.JSONDecodeError):
        completion("I am afraid I cannot help with that.").json()


def test_token_totals():
    result = Completion(text="{}", prompt_tokens=120, completion_tokens=30)
    assert result.total_tokens == 150
