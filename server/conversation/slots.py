"""Derive conversation slots from the previous query's AST.

The obvious approach is to ask the model to report what it is carrying forward
in a structured field. That was rejected: a 7B model self-reporting its own
state is exactly the kind of thing it does unreliably, and a state object that
silently drifts from the query that actually ran is worse than no state object
at all -- the UI would then show the user something untrue.

The last validated SQL is already a precise, machine-readable statement of what
was asked. Parsing it with sqlglot gives the same information deterministically,
costs no tokens and no extra round trip, and guarantees that the state inspector
in the UI shows what the agent is genuinely carrying rather than what it claims
to be carrying.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

DIALECT = "postgres"

DATE_COLUMNS = {
    "sown_date", "harvest_date", "recorded_at", "issued_at",
    "acknowledged_at", "visited_at", "registered_on", "joined_on",
}

# Predicates that exist to clean the data rather than to express user intent.
# They are re-applied by the rules on every turn, so carrying them as "filters"
# would clutter the state inspector with noise the user never asked for.
HYGIENE_VALUES = {"-273", "-99", "500", "999.9"}


@dataclass
class Slots:
    """What the agent is carrying forward, derived from the last query."""

    metric: str | None = None
    entity_filters: dict[str, str] = field(default_factory=dict)
    time_filters: list[str] = field(default_factory=list)
    grouping: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (self.metric, self.entity_filters, self.time_filters,
             self.grouping, self.tables)
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "entity_filters": self.entity_filters,
            "time_filters": self.time_filters,
            "grouping": self.grouping,
            "tables": self.tables,
        }

    def describe(self) -> str:
        """One-line-per-slot summary, for the prompt and the UI inspector."""
        if self.is_empty():
            return "(nothing carried forward)"
        parts = []
        if self.metric:
            parts.append(f"measuring: {self.metric}")
        if self.entity_filters:
            joined = ", ".join(f"{k} = {v}" for k, v in self.entity_filters.items())
            parts.append(f"filtered to: {joined}")
        if self.time_filters:
            parts.append(f"time range: {'; '.join(self.time_filters)}")
        if self.grouping:
            parts.append(f"grouped by: {', '.join(self.grouping)}")
        if self.tables:
            parts.append(f"tables: {', '.join(self.tables)}")
        return "\n".join(f"- {p}" for p in parts)


def derive(sql: str) -> Slots:
    """Extract slots from a validated SELECT. Never raises on odd input."""
    try:
        tree = sqlglot.parse_one(sql, dialect=DIALECT)
    except Exception:  # noqa: BLE001 - a state summary must never break a turn
        return Slots()
    if tree is None:
        return Slots()

    return Slots(
        metric=_metric(tree),
        entity_filters=_entity_filters(tree),
        time_filters=_time_filters(tree),
        grouping=_grouping(tree),
        tables=sorted({t.name for t in tree.find_all(exp.Table) if t.name}),
    )


def _metric(tree: exp.Expression) -> str | None:
    """The first aggregate in the SELECT list, as written."""
    select = tree.find(exp.Select)
    if select is None:
        return None
    for projection in select.expressions:
        target = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(target, exp.AggFunc):
            name = projection.alias if isinstance(projection, exp.Alias) else None
            written = target.sql(dialect=DIALECT)
            return f"{written} AS {name}" if name else written
    return None


def _predicates(tree: exp.Expression) -> list[exp.Expression]:
    where = tree.find(exp.Where)
    return list(where.find_all(exp.Binary, exp.In, exp.Between)) if where else []


def _entity_filters(tree: exp.Expression) -> dict[str, str]:
    """Equality and IN predicates on non-date columns, as user-facing filters."""
    out: dict[str, str] = {}
    where = tree.find(exp.Where)
    if where is None:
        return out

    for node in where.find_all(exp.EQ, exp.In):
        column = node.this if isinstance(node.this, exp.Column) else None
        if column is None or column.name.lower() in DATE_COLUMNS:
            continue
        if isinstance(node, exp.EQ):
            value = node.expression.sql(dialect=DIALECT)
        else:
            values = [e.sql(dialect=DIALECT) for e in node.expressions]
            if any(v.strip("'\"") in HYGIENE_VALUES for v in values):
                continue  # sentinel exclusion, not user intent
            value = f"IN ({', '.join(values)})"
        out[column.sql(dialect=DIALECT)] = value
    return out


def _time_filters(tree: exp.Expression) -> list[str]:
    """Any predicate touching a date or timestamp column, as written."""
    where = tree.find(exp.Where)
    if where is None:
        return []
    out: list[str] = []
    for node in where.find_all(exp.Binary, exp.Between):
        columns = [c for c in node.find_all(exp.Column)]
        if not columns or not any(c.name.lower() in DATE_COLUMNS for c in columns):
            continue
        rendered = node.sql(dialect=DIALECT)
        if isinstance(node, (exp.And, exp.Or)):
            continue  # the individual sides are reported instead
        if rendered not in out:
            out.append(rendered)
    return out


def _grouping(tree: exp.Expression) -> list[str]:
    group = tree.find(exp.Group)
    if group is None:
        return []
    return [e.sql(dialect=DIALECT) for e in group.expressions]
