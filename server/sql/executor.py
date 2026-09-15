"""Execute validated SQL against the read-only role.

Guardrail layer 2. Everything here assumes the SQL has already passed the
validator; the protections in this module are the ones that must hold even if
the validator has a bug -- a role with no write grant, a read-only transaction,
a statement timeout and a hard row cap.
"""

from __future__ import annotations

import decimal
import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

# Rejecting an expensive plan before running it is cheaper than waiting for the
# statement timeout to fire, and gives the model an error it can act on.
MAX_PLAN_COST = 5_000_000.0
MAX_RETURNED_ROWS = 1000


class ExecutionError(Exception):
    """Database-level failure. The message is fed back into the repair loop."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Column:
    name: str
    type: str


@dataclass
class ResultSet:
    columns: list[Column] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    duration_ms: int = 0
    plan_cost: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "columns": [{"name": c.name, "type": c.type} for c in self.columns],
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
        }


def _jsonable(value: Any) -> Any:
    """Serialise database values losslessly enough for JSON and comparison."""
    if isinstance(value, decimal.Decimal):
        # float() would lose precision on money-like values; the eval comparator
        # parses these back as Decimal.
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    return value


class Executor:
    """Owns the read-only connection pool used for model-generated SQL.

    Connections are pooled and EXPLAIN runs on the same connection as the query
    it is checking. The first version opened a fresh connection for the plan
    check and another for the query itself; on a loaded laptop that handshake
    cost an order of magnitude more than the queries do.
    """

    def __init__(
        self,
        dsn: str,
        *,
        statement_timeout_ms: int = 5000,
        min_size: int = 1,
        max_size: int = 4,
    ) -> None:
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=True,
            kwargs={"row_factory": tuple_row, "autocommit": False},
            configure=self._configure,
        )

    def _configure(self, conn: psycopg.Connection) -> None:
        # Belt and braces on top of the role's own settings: if the DSN ever
        # points at a more privileged role by mistake, the session is still
        # read-only and still bounded.
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
        conn.commit()

    def close(self) -> None:
        self._pool.close()

    def _explain(self, cur, sql: str) -> float:
        try:
            cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
            cost = float(cur.fetchone()[0][0]["Plan"]["Total Cost"])
        except psycopg.Error as exc:
            raise ExecutionError(_pg_code(exc), _clean(exc)) from exc
        if cost > MAX_PLAN_COST:
            raise ExecutionError(
                "plan_too_expensive",
                f"Estimated query cost {cost:,.0f} exceeds the limit of "
                f"{MAX_PLAN_COST:,.0f}. Add a filter or aggregate instead of "
                "scanning the whole table.",
            )
        return cost

    def explain(self, sql: str) -> float:
        """Planner cost estimate for `sql`, rejecting runaway plans."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            return self._explain(cur, sql)

    def run(self, sql: str, *, check_cost: bool = True) -> ResultSet:
        started = time.perf_counter()
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cost = self._explain(cur, sql) if check_cost else None
                query_started = time.perf_counter()
                cur.execute(sql)
                description = cur.description or []
                columns = [
                    Column(name=d.name, type=_type_name(conn, d.type_code))
                    for d in description
                ]
                fetched = cur.fetchmany(MAX_RETURNED_ROWS + 1)
        except psycopg.errors.QueryCanceled as exc:
            raise ExecutionError(
                "timeout",
                f"Query exceeded the {self.statement_timeout_ms} ms time limit.",
            ) from exc
        except psycopg.Error as exc:
            raise ExecutionError(_pg_code(exc), _clean(exc)) from exc

        truncated = len(fetched) > MAX_RETURNED_ROWS
        rows = [[_jsonable(v) for v in row] for row in fetched[:MAX_RETURNED_ROWS]]
        return ResultSet(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            duration_ms=int((time.perf_counter() - query_started) * 1000),
            plan_cost=cost,
        )


_TYPE_CACHE: dict[int, str] = {}


def _type_name(conn: psycopg.Connection, oid: int) -> str:
    if oid not in _TYPE_CACHE:
        with conn.cursor() as cur:
            cur.execute("SELECT typname FROM pg_type WHERE oid = %s", (oid,))
            row = cur.fetchone()
        _TYPE_CACHE[oid] = row[0] if row else str(oid)
    return _TYPE_CACHE[oid]


def _pg_code(exc: psycopg.Error) -> str:
    return getattr(getattr(exc, "diag", None), "sqlstate", None) or "db_error"


def _clean(exc: psycopg.Error) -> str:
    """First line of the database error -- enough for the model, no stack noise."""
    return str(exc).strip().split("\n")[0]
