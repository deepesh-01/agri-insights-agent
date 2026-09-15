"""Generate server/catalog.yaml from the live database plus the hand overlay.

The catalog is the single source of truth read by three consumers: the prompt
builder, the SQL validator's identifier allowlist, and the UI schema panel.
Generating it from PostgreSQL introspection means it cannot drift from the
schema; the overlay carries only what introspection cannot know (join paths,
business rules, ambiguity policy).

Usage:  uv run python server/build_catalog.py [--dsn ...] [--out server/catalog.yaml]
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

import psycopg
import yaml

SCHEMA = "agri"

# Columns whose distinct values are small enough to be worth shipping verbatim
# in the prompt. Enumerating a high-cardinality column would blow the budget.
ENUM_MAX_CARDINALITY = 24


def _sql_literal(v) -> str:
    """Render a value the way it must appear inside SQL."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def introspect(cur) -> dict:
    cur.execute(
        """
        SELECT c.relname,
               obj_description(c.oid, 'pg_class'),
               (SELECT reltuples::bigint FROM pg_class WHERE oid = c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind = 'r'
        ORDER BY c.relname
        """,
        (SCHEMA,),
    )
    tables = {name: {"description": desc, "approx_rows": rows, "columns": {}}
              for name, desc, rows in cur.fetchall()}

    cur.execute(
        """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
               a.attnotnull, col_description(c.oid, a.attnum)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind = 'r' AND a.attnum > 0
              AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
        """,
        (SCHEMA,),
    )
    for table, col, typ, notnull, desc in cur.fetchall():
        tables[table]["columns"][col] = {
            "type": typ,
            "nullable": not notnull,
            "description": desc,
        }

    # Primary and foreign keys.
    cur.execute(
        """
        SELECT con.contype, c.relname, con.conname,
               pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND con.contype IN ('p', 'f')
        ORDER BY c.relname, con.contype DESC
        """,
        (SCHEMA,),
    )
    for kind, table, _name, definition in cur.fetchall():
        key = "primary_key" if kind == "p" else "foreign_keys"
        if kind == "p":
            tables[table][key] = definition.replace("PRIMARY KEY ", "").strip("()")
        else:
            tables[table].setdefault(key, []).append(definition.replace("FOREIGN KEY ", ""))
    return tables


def exact_counts(cur, tables: dict) -> None:
    for name in tables:
        cur.execute(f'SELECT count(*) FROM {SCHEMA}."{name}"')
        tables[name]["rows"] = cur.fetchone()[0]
        tables[name].pop("approx_rows", None)


def enum_domains(cur, tables: dict, free_text: set[str]) -> None:
    """Attach the observed value domain to every low-cardinality text column."""
    for table, meta in tables.items():
        for col, cmeta in meta["columns"].items():
            if cmeta["type"] not in ("text", "boolean"):
                continue
            if f"{table}.{col}" in free_text:
                cmeta["free_text"] = True
                continue
            cur.execute(
                f'SELECT count(DISTINCT "{col}") FROM {SCHEMA}."{table}"'
            )
            n = cur.fetchone()[0]
            if n > ENUM_MAX_CARDINALITY:
                cmeta["distinct_values"] = n
                continue
            cur.execute(
                f'SELECT "{col}", count(*) FROM {SCHEMA}."{table}" '
                f'WHERE "{col}" IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1'
            )
            cmeta["values"] = {_sql_literal(v): c for v, c in cur.fetchall()}


def null_counts(cur, tables: dict) -> None:
    for table, meta in tables.items():
        for col, cmeta in meta["columns"].items():
            if not cmeta["nullable"]:
                continue
            cur.execute(
                f'SELECT count(*) FROM {SCHEMA}."{table}" WHERE "{col}" IS NULL'
            )
            cmeta["nulls"] = cur.fetchone()[0]


def numeric_ranges(cur, tables: dict) -> None:
    for table, meta in tables.items():
        for col, cmeta in meta["columns"].items():
            if not cmeta["type"].startswith(("numeric", "integer", "bigint")):
                continue
            if col == "id" or col.endswith("_id"):
                continue
            cur.execute(
                f'SELECT min("{col}"), max("{col}") FROM {SCHEMA}."{table}"'
            )
            lo, hi = cur.fetchone()
            cmeta["range"] = [float(lo), float(hi)]


def coverage(cur, tables: dict) -> dict:
    """MIN/MAX of every date column, split into observed vs forward-looking.

    Some columns record things that happened; others record things that are
    planned. crop_cycle.sown_date holds 59 rows dated after today. Collapsing
    both kinds into one "latest data date" would tell the user the data runs to
    November 2026 when the last actual observation is three months earlier, so
    the two are separated here and a column is classified by the data itself
    rather than by a hand-maintained list.
    """
    out: dict[str, dict] = {}
    for table, meta in tables.items():
        for col, cmeta in meta["columns"].items():
            if not cmeta["type"].startswith(("date", "timestamp")):
                continue
            cur.execute(
                f'SELECT min("{col}"), max("{col}"), '
                f'count(*) FILTER (WHERE "{col}" > CURRENT_DATE) '
                f'FROM {SCHEMA}."{table}"'
            )
            lo, hi, future = cur.fetchone()
            if lo is None:
                continue
            span = {"min": str(lo), "max": str(hi)}
            if future:
                span["rows_in_future"] = future
                span["forward_looking"] = True
            cmeta["coverage"] = span
            out[f"{table}.{col}"] = span
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql:///agri_insights")
    ap.add_argument("--overlay", default="server/catalog_overlay.yaml")
    ap.add_argument("--out", default="server/catalog.yaml")
    args = ap.parse_args()

    overlay = yaml.safe_load(pathlib.Path(args.overlay).read_text())
    free_text = set(overlay.get("free_text_columns", []))

    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        tables = introspect(cur)
        exact_counts(cur, tables)
        enum_domains(cur, tables, free_text)
        null_counts(cur, tables)
        numeric_ranges(cur, tables)
        cov = coverage(cur, tables)

    catalog = {
        "catalog_version": overlay["version"],
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "schema": SCHEMA,
        "data_coverage": cov,
        "latest_observation_date": max(
            v["max"] for v in cov.values() if not v.get("forward_looking")
        )[:10],
        "forward_looking_columns": sorted(
            k for k, v in cov.items() if v.get("forward_looking")
        ),
        "tables": tables,
        "join_paths": overlay["join_paths"],
        "join_notes": overlay["notes"],
        "business_rules": overlay["business_rules"],
        "ambiguities": overlay["ambiguities"],
    }

    out = pathlib.Path(args.out)
    out.write_text(
        "# GENERATED by server/build_catalog.py -- do not edit by hand.\n"
        "# Schema facts come from the live database; business rules, join paths\n"
        "# and ambiguity policy come from server/catalog_overlay.yaml.\n"
        + yaml.safe_dump(catalog, sort_keys=False, width=88, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"wrote {out}  tables={len(tables)}  coverage_cols={len(cov)}  "
          f"latest_observation_date={catalog['latest_observation_date']}  "
          f"forward_looking={catalog['forward_looking_columns']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
