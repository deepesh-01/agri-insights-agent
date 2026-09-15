"""Guardrail tests.

These carry more weight than the rest of the suite: the validator is the
component whose failure mode is a destructive query reaching the database.
"""

from __future__ import annotations

import pytest

from server.catalog import load
from server.sql.validator import (
    DEFAULT_ROW_LIMIT,
    MAX_ROW_LIMIT,
    ValidationError,
    validate,
)


@pytest.fixture(scope="module")
def catalog():
    return load()


def ok(sql, catalog):
    return validate(sql, catalog)


def rejected(sql, catalog) -> str:
    with pytest.raises(ValidationError) as exc:
        validate(sql, catalog)
    return exc.value.code


# ---------------------------------------------------------------- destructive --
@pytest.mark.parametrize("sql", [
    "DELETE FROM farmer",
    "DROP TABLE farmer",
    "TRUNCATE TABLE farmer",
    "UPDATE farmer SET is_active = false",
    "INSERT INTO farmer (id, name) VALUES (1, 'x')",
    "ALTER TABLE farmer ADD COLUMN x int",
    "CREATE TABLE evil (i int)",
    "GRANT SELECT ON farmer TO public",
])
def test_destructive_statements_are_rejected(sql, catalog):
    assert rejected(sql, catalog) in {"forbidden_statement", "not_a_select"}


def test_statement_chaining_is_rejected(catalog):
    assert rejected("SELECT 1 FROM farmer; DROP TABLE farmer", catalog) == "multiple_statements"


def test_destructive_hidden_in_a_cte_is_rejected(catalog):
    sql = "WITH x AS (SELECT 1 FROM farmer) DELETE FROM farmer"
    assert rejected(sql, catalog) in {"forbidden_statement", "not_a_select"}


def test_comment_smuggled_semicolon_is_rejected(catalog):
    sql = "SELECT id FROM farmer WHERE name = 'a'; DELETE FROM farmer --'"
    assert rejected(sql, catalog) == "multiple_statements"


# ------------------------------------------------------------------ exfiltration --
@pytest.mark.parametrize("sql,code", [
    ("SELECT * FROM pg_catalog.pg_user", "forbidden_schema"),
    ("SELECT * FROM information_schema.tables", "forbidden_schema"),
    ("SELECT pg_read_file('/etc/passwd') FROM farmer", "forbidden_function"),
    ("SELECT pg_sleep(60) FROM farmer", "forbidden_function"),
    ("SELECT lo_import('/etc/passwd') FROM farmer", "forbidden_function"),
])
def test_exfiltration_and_dos_are_rejected(sql, code, catalog):
    assert rejected(sql, catalog) == code


def test_query_reading_no_table_is_rejected(catalog):
    assert rejected("SELECT 1", catalog) == "no_tables"


# ------------------------------------------------------------------ hallucination --
def test_unknown_table_is_rejected_with_the_real_list(catalog):
    with pytest.raises(ValidationError) as exc:
        validate("SELECT * FROM harvests", catalog)
    assert exc.value.code == "unknown_table"
    assert "crop_cycle" in exc.value.message  # the error must be actionable


def test_unknown_column_is_rejected(catalog):
    assert rejected("SELECT yield_kg FROM crop_cycle", catalog) == "unknown_column"


def test_unknown_column_on_a_known_table_is_rejected(catalog):
    assert rejected("SELECT c.yield_kg FROM crop_cycle c", catalog) == "unknown_column"


def test_column_from_a_table_not_in_scope_is_rejected(catalog):
    # soil_type is real, but plot is not joined here.
    assert rejected("SELECT soil_type FROM farmer", catalog) == "column_not_in_scope"


def test_unknown_alias_is_rejected(catalog):
    assert rejected("SELECT z.id FROM farmer f", catalog) == "unknown_alias"


# -------------------------------------------------------------------- valid SQL --
def test_simple_select_passes(catalog):
    assert ok("SELECT id, name FROM farmer", catalog).tables == {"farmer"}


def test_join_passes_and_reports_tables(catalog):
    sql = """
        SELECT p.district, avg(c.actual_yield / p.area_hectares) AS yield_per_ha
        FROM crop_cycle c
        JOIN plot p ON p.id = c.plot_id
        WHERE c.actual_yield IS NOT NULL
        GROUP BY p.district
    """
    assert ok(sql, catalog).tables == {"crop_cycle", "plot"}


def test_select_alias_is_usable_in_order_by(catalog):
    sql = """
        SELECT p.district, count(*) AS n
        FROM plot p GROUP BY p.district ORDER BY n DESC
    """
    ok(sql, catalog)


def test_cte_columns_are_not_checked_against_the_catalog(catalog):
    sql = """
        WITH per_plot AS (
            SELECT plot_id, avg(value) AS mean_value
            FROM sensor_reading
            WHERE reading_type = 'soil_moisture'
              AND value NOT IN (-273, -99, 500, 999.9)
            GROUP BY plot_id
        )
        SELECT pp.plot_id, pp.mean_value FROM per_plot pp
    """
    ok(sql, catalog)


def test_window_function_passes(catalog):
    sql = """
        SELECT c.crop, c.actual_yield,
               rank() OVER (PARTITION BY c.crop ORDER BY c.actual_yield DESC) AS rk
        FROM crop_cycle c
        WHERE c.actual_yield IS NOT NULL
    """
    ok(sql, catalog)


def test_union_passes(catalog):
    sql = "SELECT district FROM plot UNION SELECT district FROM farmer"
    ok(sql, catalog)


# ------------------------------------------------------------------ row capping --
def test_missing_limit_is_injected(catalog):
    result = ok("SELECT id FROM farmer", catalog)
    assert result.limit_applied == DEFAULT_ROW_LIMIT
    assert f"LIMIT {DEFAULT_ROW_LIMIT}" in result.sql.upper()


def test_small_explicit_limit_is_left_alone(catalog):
    result = ok("SELECT id FROM farmer LIMIT 10", catalog)
    assert result.limit_applied is None
    assert "LIMIT 10" in result.sql.upper()


def test_oversized_limit_is_clamped(catalog):
    result = ok("SELECT id FROM farmer LIMIT 999999", catalog)
    assert result.limit_applied == MAX_ROW_LIMIT
    assert f"LIMIT {MAX_ROW_LIMIT}" in result.sql.upper()


def test_empty_sql_is_rejected(catalog):
    assert rejected("   ", catalog) == "empty"


def test_unparseable_sql_is_rejected(catalog):
    assert rejected("SELECT FROM WHERE tastes like", catalog) == "parse_error"
