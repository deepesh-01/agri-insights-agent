"""Load the sample CSV extracts into PostgreSQL.

Data is loaded RAW. The sentinel sensor values, duplicate readings and mixed
yield units are part of the exercise -- the agent is expected to handle them
using the schema comments, so the ETL must not quietly fix them.

Usage:  python db/load.py [--dsn postgresql://...] [--csv-dir CSV/csv]
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys

import psycopg

# Column order per table, and which columns may be empty in the CSV.
TABLES: dict[str, tuple[list[str], set[str]]] = {
    "farmer": (["id", "name", "district", "state", "registered_on", "is_active"], set()),
    "field_agent": (["id", "name", "district", "joined_on"], set()),
    "plot": (["id", "farmer_id", "area_hectares", "soil_type", "district", "irrigation_type"], set()),
    "crop_cycle": (
        ["id", "plot_id", "crop", "season", "sown_date", "harvest_date",
         "expected_yield", "actual_yield", "status"],
        {"harvest_date", "actual_yield"},
    ),
    "sensor_reading": (["id", "plot_id", "reading_type", "value", "recorded_at"], set()),
    "advisory": (
        ["id", "plot_id", "issued_at", "category", "severity", "acknowledged_at"],
        {"acknowledged_at"},
    ),
    "field_visit": (
        ["id", "plot_id", "agent_id", "visited_at", "outcome", "notes"],
        {"notes"},
    ),
}

# Insertion order respects foreign keys.
ORDER = ["farmer", "field_agent", "plot", "crop_cycle", "sensor_reading",
         "advisory", "field_visit"]

BOOLS = {"TRUE": True, "FALSE": False, "true": True, "false": False}


def rows_for(path: pathlib.Path, cols: list[str], nullable: set[str]):
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        if header != cols:
            raise SystemExit(f"{path.name}: header {header} != expected {cols}")
        for line_no, raw in enumerate(reader, start=2):
            out = []
            for col in cols:
                val = (raw[col] or "").strip()
                if val == "":
                    if col in nullable:
                        out.append(None)
                        continue
                    raise SystemExit(f"{path.name}:{line_no}: empty value in non-nullable {col}")
                out.append(BOOLS.get(val, val) if col == "is_active" else val)
            yield out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql:///agri_insights")
    ap.add_argument("--csv-dir", default="CSV/csv")
    ap.add_argument("--schema", default="db/schema.sql")
    args = ap.parse_args()

    csv_dir = pathlib.Path(args.csv_dir)
    schema_sql = pathlib.Path(args.schema).read_text(encoding="utf-8")

    with psycopg.connect(args.dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
            print(f"schema  applied from {args.schema}")

            for table in ORDER:
                cols, nullable = TABLES[table]
                path = csv_dir / f"{table}.csv"
                collist = ", ".join(cols)
                copy_sql = f"COPY agri.{table} ({collist}) FROM STDIN"
                count = 0
                with cur.copy(copy_sql) as copy:
                    for row in rows_for(path, cols, nullable):
                        copy.write_row(row)
                        count += 1
                print(f"{table:16} {count:>6} rows")

            cur.execute("ANALYZE")
        conn.commit()

    # Verify against the database rather than trusting the counters above.
    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        print("\nverification")
        for table in ORDER:
            cur.execute(f"SELECT count(*) FROM agri.{table}")
            print(f"  {table:16} {cur.fetchone()[0]:>6}")
        cur.execute("""
            SELECT count(*) FROM agri.sensor_reading
            WHERE value IN (-273, -99, 500, 999.9)
        """)
        print(f"  sentinel rows preserved: {cur.fetchone()[0]}")
        cur.execute("""
            SELECT count(*) FROM (
                SELECT plot_id, reading_type, recorded_at
                FROM agri.sensor_reading
                GROUP BY 1, 2, 3 HAVING count(*) > 1
            ) d
        """)
        print(f"  duplicate reading keys preserved: {cur.fetchone()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
