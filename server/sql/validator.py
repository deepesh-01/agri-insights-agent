"""Static validation of model-generated SQL.

The brief is explicit that a prompt asking the model to only write SELECT
statements scores zero: guardrails must be enforced outside the model. This
module is that enforcement, and it runs before the database is touched at all.

It is layer 1 of two. Layer 2 is the `agent_ro` PostgreSQL role, which holds
SELECT and nothing else. Either layer alone is sufficient to stop a write; the
validator exists because a clean rejection with a reason the model can act on
is far more useful than a permission error, and because it catches the much
more common failure -- a hallucinated column -- which the database would only
report after a round trip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DIALECT = "postgres"
DEFAULT_ROW_LIMIT = 1000
MAX_ROW_LIMIT = 5000

# Statement types that must never reach the database, even though the role
# would refuse them anyway. `exp.Command` catches everything sqlglot parses as
# an opaque command, which includes SET, VACUUM, CALL and friends.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Grant, exp.Command, exp.Copy, exp.Merge, exp.TruncateTable,
)

# Functions and namespaces that read the filesystem, reach the network, stall
# the server or expose the catalog.
FORBIDDEN_FUNCTIONS = frozenset({
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "lo_import", "lo_export", "dblink", "dblink_exec",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "query_to_xml", "pg_logical_emit_message", "set_config",
    "current_setting", "pg_read_server_files",
})
FORBIDDEN_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})
FORBIDDEN_TABLE_PREFIX = "pg_"


class ValidationError(Exception):
    """Rejected SQL. The message is fed back to the model in the repair loop."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ValidationResult:
    sql: str                       # normalised, limit-enforced SQL to execute
    tables: set[str] = field(default_factory=set)
    limit_applied: int | None = None
    notes: list[str] = field(default_factory=list)


def validate(sql: str, catalog) -> ValidationResult:
    """Validate and normalise `sql`. Raises ValidationError on rejection."""
    if not sql or not sql.strip():
        raise ValidationError("empty", "No SQL was produced.")

    statements = _parse(sql)
    if len(statements) != 1:
        raise ValidationError(
            "multiple_statements",
            f"Expected exactly one statement, found {len(statements)}. "
            "Chaining statements with ';' is not allowed.",
        )
    tree = statements[0]

    _reject_forbidden_nodes(tree)
    _require_select(tree)
    _reject_forbidden_functions(tree)

    tables = _collect_real_tables(tree, catalog)
    _check_columns(tree, catalog)

    tree, limit = _enforce_limit(tree)
    return ValidationResult(
        sql=tree.sql(dialect=DIALECT, pretty=True),
        tables=tables,
        limit_applied=limit,
    )


# --------------------------------------------------------------------- parse --
def _parse(sql: str) -> list[exp.Expression]:
    try:
        parsed = sqlglot.parse(sql, dialect=DIALECT)
    except Exception as exc:  # sqlglot raises several types
        raise ValidationError("parse_error", f"SQL did not parse: {exc}") from exc
    statements = [s for s in parsed if s is not None]
    if not statements:
        raise ValidationError("parse_error", "SQL did not parse into a statement.")
    return statements


def _require_select(tree: exp.Expression) -> None:
    root = tree
    # A statement may be wrapped: WITH ... SELECT, (SELECT ...), UNION of SELECTs.
    if isinstance(root, exp.Subquery):
        root = root.this
    if isinstance(root, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        return
    raise ValidationError(
        "not_a_select",
        f"Only SELECT queries are allowed; got {type(root).__name__.upper()}.",
    )


def _reject_forbidden_nodes(tree: exp.Expression) -> None:
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise ValidationError(
                "forbidden_statement",
                f"{type(node).__name__.upper()} is not allowed. "
                "This database is read-only; only SELECT queries are permitted.",
            )


def _reject_forbidden_functions(tree: exp.Expression) -> None:
    for node in tree.walk():
        name = None
        if isinstance(node, exp.Anonymous):
            name = str(node.this).lower()
        elif isinstance(node, exp.Func):
            name = node.sql_name().lower()
        if name and name in FORBIDDEN_FUNCTIONS:
            raise ValidationError(
                "forbidden_function",
                f"Function {name}() is not allowed.",
            )


# -------------------------------------------------------------------- tables --
def _cte_names(tree: exp.Expression) -> set[str]:
    return {
        cte.alias_or_name.lower()
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name
    }


def _collect_real_tables(tree: exp.Expression, catalog) -> set[str]:
    """Every physical table referenced must exist in the catalog."""
    ctes = _cte_names(tree)
    found: set[str] = set()

    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        schema = (table.db or "").lower()
        if not name:
            continue
        if name in ctes and not schema:
            continue  # reference to a CTE defined in this query

        if schema and schema in FORBIDDEN_SCHEMAS:
            raise ValidationError(
                "forbidden_schema",
                f"Schema {schema} is not accessible.",
            )
        if name.startswith(FORBIDDEN_TABLE_PREFIX):
            raise ValidationError(
                "forbidden_table",
                f"System table {name} is not accessible.",
            )
        if name not in catalog.allowed_tables:
            raise ValidationError(
                "unknown_table",
                f"Table '{name}' does not exist. "
                f"Available tables: {', '.join(sorted(catalog.allowed_tables))}.",
            )
        if schema and schema != catalog.schema:
            raise ValidationError(
                "forbidden_schema",
                f"Schema {schema} is not accessible; use {catalog.schema}.",
            )
        found.add(name)

    if not found:
        raise ValidationError(
            "no_tables",
            "The query does not read any table in the database.",
        )
    return found


# ------------------------------------------------------------------- columns --
def _alias_map(scope: exp.Expression, catalog) -> dict[str, str | None]:
    """alias (or table name) -> physical table, or None if not resolvable.

    None means the alias refers to a CTE or a subquery, whose output columns
    are computed rather than catalogued, so columns qualified by it cannot be
    checked against the catalog and are skipped rather than wrongly rejected.
    """
    out: dict[str, str | None] = {}
    for table in scope.find_all(exp.Table):
        name = (table.name or "").lower()
        alias = (table.alias or name).lower()
        out[alias] = name if name in catalog.allowed_tables else None
    for sub in scope.find_all(exp.Subquery):
        if sub.alias:
            out[sub.alias.lower()] = None
    for cte in scope.find_all(exp.CTE):
        if cte.alias_or_name:
            out[cte.alias_or_name.lower()] = None
    return out


def _output_aliases(tree: exp.Expression) -> set[str]:
    """Names introduced by the query itself: SELECT aliases, CTE column lists."""
    names: set[str] = set()
    for alias in tree.find_all(exp.Alias):
        if alias.alias:
            names.add(alias.alias.lower())
    for cte in tree.find_all(exp.CTE):
        if isinstance(cte.args.get("alias"), exp.TableAlias):
            for col in cte.args["alias"].columns:
                names.add(col.name.lower())
    return names


def _check_columns(tree: exp.Expression, catalog) -> None:
    aliases = _alias_map(tree, catalog)
    produced = _output_aliases(tree)
    # Tables actually in scope; an unqualified column must belong to one of them.
    in_scope = {t for t in aliases.values() if t}

    for col in tree.find_all(exp.Column):
        name = (col.name or "").lower()
        if not name or name == "*":
            continue
        qualifier = (col.table or "").lower()

        if qualifier:
            target = aliases.get(qualifier, "__unknown__")
            if target is None:
                continue  # CTE or subquery output: not catalogued
            if target == "__unknown__":
                raise ValidationError(
                    "unknown_alias",
                    f"'{qualifier}' in '{qualifier}.{name}' is not a table or "
                    "alias used in this query.",
                )
            if (target, name) not in catalog.qualified_columns:
                raise ValidationError(
                    "unknown_column",
                    f"Column '{name}' does not exist on table '{target}'. "
                    f"{target} has: {', '.join(catalog.columns_of(target))}.",
                )
        else:
            if name in produced:
                continue
            if name in catalog.allowed_columns:
                # Exists somewhere; confirm it exists on a table in scope.
                if in_scope and not any(
                    (t, name) in catalog.qualified_columns for t in in_scope
                ):
                    raise ValidationError(
                        "column_not_in_scope",
                        f"Column '{name}' exists in the schema but not on any "
                        f"table in this query ({', '.join(sorted(in_scope))}).",
                    )
                continue
            raise ValidationError(
                "unknown_column",
                f"Column '{name}' does not exist in the schema.",
            )


# --------------------------------------------------------------------- limit --
def _enforce_limit(tree: exp.Expression) -> tuple[exp.Expression, int | None]:
    """Guarantee a bounded result set. Returns the tree and the limit imposed."""
    root = tree.this if isinstance(tree, exp.Subquery) else tree
    if not isinstance(root, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        return tree, None

    existing = root.args.get("limit")
    if existing is None:
        return root.limit(DEFAULT_ROW_LIMIT), DEFAULT_ROW_LIMIT

    try:
        value = int(existing.expression.name)
    except (AttributeError, TypeError, ValueError):
        # A non-literal LIMIT (parameter, expression) is not something we can
        # reason about, so replace it with the cap rather than trusting it.
        return root.limit(DEFAULT_ROW_LIMIT), DEFAULT_ROW_LIMIT

    if value > MAX_ROW_LIMIT:
        return root.limit(MAX_ROW_LIMIT), MAX_ROW_LIMIT
    return root, None
