"""Result-set comparison for execution accuracy.

The brief rules out string comparison of SQL, and for good reason: there are
many correct spellings of the same query. Two queries are equivalent here if
running them produces the same data.

The comparison has to be strict enough to catch a wrong answer and lenient
enough not to punish a different-but-correct one. The rules, and why:

- Column NAMES are ignored, positions are compared. `AS n` versus `AS count`
  is a labelling choice, not a correctness one. Column COUNT must match.
- Row ORDER is ignored unless the gold query has an ORDER BY, in which case the
  question asked for an ordering and the ordering is part of the answer.
- Numbers compare with a relative tolerance. Postgres `numeric` and a float
  round-trip differ in the last bits, and that is not a wrong answer.
- Everything else compares on its string form, so a date and its ISO string are
  the same value.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Sequence

REL_TOLERANCE = 1e-6
ABS_TOLERANCE = 1e-9
ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)


@dataclass
class Comparison:
    match: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.match


def gold_is_ordered(sql: str) -> bool:
    """True when the gold query's own ORDER BY makes row order meaningful."""
    return bool(ORDER_BY.search(sql or ""))


def _norm_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        return text.lower()


def _scalars_equal(a: Any, b: Any) -> bool:
    a, b = _norm_scalar(a), _norm_scalar(b)
    if a is None or b is None:
        return a is b
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=REL_TOLERANCE, abs_tol=ABS_TOLERANCE)
    return a == b


def _row_key(row: Sequence[Any]) -> tuple:
    """Stable sort key for order-insensitive comparison."""
    out = []
    for value in row:
        norm = _norm_scalar(value)
        if norm is None:
            out.append((0, 0.0, ""))
        elif isinstance(norm, float):
            out.append((1, norm, ""))
        else:
            out.append((2, 0.0, str(norm)))
    return tuple(out)


def compare(
    gold_rows: list[Sequence[Any]],
    got_rows: list[Sequence[Any]],
    *,
    ordered: bool,
) -> Comparison:
    if len(gold_rows) != len(got_rows):
        return Comparison(
            False, f"row count differs: expected {len(gold_rows)}, got {len(got_rows)}"
        )
    if not gold_rows:
        return Comparison(True, "both empty")

    gold_width = len(gold_rows[0])
    got_width = len(got_rows[0])
    if gold_width != got_width:
        return Comparison(
            False, f"column count differs: expected {gold_width}, got {got_width}"
        )

    left, right = list(gold_rows), list(got_rows)
    if not ordered:
        left = sorted(left, key=_row_key)
        right = sorted(right, key=_row_key)

    for index, (gold_row, got_row) in enumerate(zip(left, right)):
        for column, (a, b) in enumerate(zip(gold_row, got_row)):
            if not _scalars_equal(a, b):
                return Comparison(
                    False,
                    f"row {index} column {column} differs: "
                    f"expected {a!r}, got {b!r}",
                )
    return Comparison(True, "identical")
