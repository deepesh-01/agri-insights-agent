"""Executor tests — guardrail layer 2 against the real database.

These run against the live `agent_ro` role rather than a mock, because the
thing being tested is precisely that PostgreSQL refuses, not that a mock says
it would.
"""

from __future__ import annotations

import pytest

from server.config import Settings
from server.sql.executor import MAX_PLAN_COST, ExecutionError, Executor


@pytest.fixture(scope="module")
def executor():
    ex = Executor(Settings().agent_dsn)
    yield ex
    ex.close()


# ------------------------------------------------------------- read-only --
@pytest.mark.parametrize("sql", [
    "DELETE FROM farmer",
    "DROP TABLE farmer",
    "CREATE TABLE evil (i int)",
    "UPDATE farmer SET is_active = false",
])
def test_the_database_itself_refuses_writes(executor, sql):
    """Layer 2 holds even if the validator were bypassed entirely."""
    with pytest.raises(ExecutionError) as exc:
        executor.run(sql, check_cost=False)
    assert "read-only" in exc.value.message.lower()


def test_the_role_cannot_create_temp_objects(executor):
    with pytest.raises(ExecutionError):
        executor.run("CREATE TEMP TABLE t AS SELECT 1", check_cost=False)


# ------------------------------------------------------------- cost gate --
def test_a_runaway_plan_is_rejected_before_it_runs(executor):
    sql = ("SELECT count(*) FROM sensor_reading a, sensor_reading b, "
           "sensor_reading c")
    with pytest.raises(ExecutionError) as exc:
        executor.run(sql)
    assert exc.value.code == "plan_too_expensive"


def test_cheap_queries_pass_the_cost_gate(executor):
    result = executor.run("SELECT count(*) AS n FROM farmer")
    assert result.plan_cost is not None and result.plan_cost < MAX_PLAN_COST
    assert result.rows == [[300]]


def test_explain_reports_a_cost(executor):
    assert executor.explain("SELECT 1 FROM farmer LIMIT 1") > 0


# ------------------------------------------------------------ result shape --
def test_columns_carry_names_and_types(executor):
    result = executor.run("SELECT id, name, registered_on FROM farmer LIMIT 1")
    assert [c.name for c in result.columns] == ["id", "name", "registered_on"]
    assert [c.type for c in result.columns] == ["int4", "text", "date"]


def test_dates_and_numerics_serialise_for_json(executor):
    result = executor.run(
        "SELECT registered_on, is_active FROM farmer ORDER BY id LIMIT 1"
    )
    registered_on, is_active = result.rows[0]
    assert isinstance(registered_on, str) and registered_on.count("-") == 2
    assert isinstance(is_active, bool)

    numeric = executor.run("SELECT area_hectares FROM plot ORDER BY id LIMIT 1")
    assert isinstance(numeric.rows[0][0], float)


def test_timestamps_serialise_as_iso(executor):
    result = executor.run(
        "SELECT recorded_at FROM sensor_reading ORDER BY id LIMIT 1"
    )
    assert "T" in result.rows[0][0]


def test_nulls_survive_as_none(executor):
    result = executor.run(
        "SELECT actual_yield FROM crop_cycle WHERE actual_yield IS NULL LIMIT 1"
    )
    assert result.rows == [[None]]


def test_row_cap_marks_truncation(executor):
    result = executor.run("SELECT id FROM sensor_reading")
    assert result.row_count == 1000
    assert result.truncated is True


def test_small_results_are_not_marked_truncated(executor):
    assert executor.run("SELECT id FROM farmer LIMIT 5").truncated is False


# ----------------------------------------------------------------- errors --
def test_unknown_column_reports_a_usable_message(executor):
    with pytest.raises(ExecutionError) as exc:
        executor.run("SELECT nope FROM farmer")
    assert "nope" in exc.value.message


def test_error_messages_are_a_single_line(executor):
    with pytest.raises(ExecutionError) as exc:
        executor.run("SELECT nope FROM farmer")
    assert "\n" not in exc.value.message


def test_statement_timeout_is_configured(executor):
    result = executor.run("SHOW statement_timeout", check_cost=False)
    assert result.rows == [["5s"]]
