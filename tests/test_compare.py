"""Tests for the execution-accuracy comparator.

A comparator that is too lenient silently inflates every number in the report,
so its own failure modes are worth testing directly.
"""

from __future__ import annotations

from eval.compare import compare, gold_is_ordered


def test_identical_rows_match():
    assert compare([[1, "a"]], [[1, "a"]], ordered=False)


def test_column_aliases_are_irrelevant():
    # Column names never reach the comparator; only positions and values do.
    assert compare([["Belgaum", 26]], [["Belgaum", 26]], ordered=False)


def test_row_order_ignored_when_gold_is_unordered():
    gold = [["a", 1], ["b", 2]]
    got = [["b", 2], ["a", 1]]
    assert compare(gold, got, ordered=False)


def test_row_order_enforced_when_gold_is_ordered():
    gold = [["a", 1], ["b", 2]]
    got = [["b", 2], ["a", 1]]
    assert not compare(gold, got, ordered=True)


def test_float_tolerance_absorbs_numeric_round_trip():
    assert compare([[1234.5678901]], [[1234.5678902]], ordered=False)


def test_real_differences_are_caught():
    assert not compare([[100.0]], [[101.0]], ordered=False)


def test_row_count_mismatch_is_reported():
    result = compare([[1], [2]], [[1]], ordered=False)
    assert not result and "row count" in result.reason


def test_column_count_mismatch_is_reported():
    result = compare([[1, 2]], [[1]], ordered=False)
    assert not result and "column count" in result.reason


def test_both_empty_matches():
    assert compare([], [], ordered=False)


def test_empty_versus_non_empty_fails():
    assert not compare([], [[1]], ordered=False)


def test_numeric_string_and_number_are_the_same_value():
    assert compare([["26"]], [[26]], ordered=False)


def test_case_insensitive_text():
    assert compare([["Belgaum"]], [["belgaum"]], ordered=False)


def test_nulls_compare_equal_and_differ_from_zero():
    assert compare([[None]], [[None]], ordered=False)
    assert not compare([[None]], [[0]], ordered=False)


def test_order_by_detection():
    assert gold_is_ordered("SELECT a FROM t ORDER BY a")
    assert gold_is_ordered("select a from t order  by a desc")
    assert not gold_is_ordered("SELECT a FROM t GROUP BY a")
