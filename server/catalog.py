"""Load the generated catalog and render it into prompt-sized views.

The raw catalog.yaml is ~4.2k tokens. An 8192-token context also has to hold
the system prompt, few-shot examples, conversation state and the model's own
output, so the prompt gets a compacted rendering rather than the whole file.
Compaction is deliberate and lossy in one direction only: every fact that
changes a query's CORRECTNESS is kept, and everything that is merely
descriptive is dropped.
"""

from __future__ import annotations

import functools
import pathlib
from dataclasses import dataclass
from typing import Any

import yaml

CATALOG_PATH = pathlib.Path(__file__).with_name("catalog.yaml")

# Column comments carry the long-form explanation of each trap. Stating a trap
# both here and in the rules block costs ~250 tokens of duplication, and that
# duplication was removed once to fit an 8B model into a 4096-token context.
# Measured effect: accuracy fell 82.4% -> 76.5% on the same conversations. The
# 8B models turned out not to fit on this hardware at any context, so the
# duplication is restored -- teaching a trap twice is worth six accuracy points.
KEEP_COMMENT_FOR = {
    ("crop_cycle", "expected_yield"),
    ("crop_cycle", "actual_yield"),
    ("sensor_reading", "value"),
}


@dataclass(frozen=True)
class Catalog:
    raw: dict[str, Any]

    # ---------------------------------------------------------------- access --
    @property
    def version(self) -> int:
        return self.raw["catalog_version"]

    @property
    def schema(self) -> str:
        return self.raw["schema"]

    @property
    def tables(self) -> dict[str, Any]:
        return self.raw["tables"]

    @property
    def latest_observation_date(self) -> str:
        return self.raw["latest_observation_date"]

    @property
    def ambiguities(self) -> list[dict[str, Any]]:
        return self.raw["ambiguities"]

    # ------------------------------------------------------------- allowlist --
    @functools.cached_property
    def allowed_tables(self) -> frozenset[str]:
        return frozenset(self.tables)

    @functools.cached_property
    def allowed_columns(self) -> frozenset[str]:
        """Every column name, unqualified. Used for bare-column references."""
        return frozenset(
            col for meta in self.tables.values() for col in meta["columns"]
        )

    @functools.cached_property
    def qualified_columns(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            (table, col)
            for table, meta in self.tables.items()
            for col in meta["columns"]
        )

    def columns_of(self, table: str) -> list[str]:
        return list(self.tables[table]["columns"])

    # ---------------------------------------------------------------- render --
    def render_schema(self) -> str:
        """Compact DDL block: types, nullability, enum domains, row counts."""
        lines: list[str] = []
        for table, meta in self.tables.items():
            cols: list[str] = []
            for col, cmeta in meta["columns"].items():
                bit = f"{col} {_short_type(cmeta['type'])}"
                if cmeta.get("nulls"):
                    bit += f" NULL[{cmeta['nulls']}]"
                if values := cmeta.get("values"):
                    bit += "{" + "|".join(values) + "}"
                cols.append(bit)
            lines.append(f"{table}({', '.join(cols)})  -- {meta['rows']} rows")

            for col, cmeta in meta["columns"].items():
                if (table, col) in KEEP_COMMENT_FOR and cmeta.get("description"):
                    text = " ".join(cmeta["description"].split())
                    lines.append(f"  ! {col}: {text}")
        return "\n".join(lines)

    def render_schema_minimal(self) -> str:
        """Bare table(column, ...) list with no types, domains or trap notes.

        Used only by the evaluation baseline, which is the brief's requested
        comparison: the same model given nothing but the table names, so the
        delta against the full system measures the engineering rather than the
        model.
        """
        return "\n".join(
            f"{table}({', '.join(meta['columns'])})"
            for table, meta in self.tables.items()
        )

    def render_keys(self) -> str:
        """FK edges as `child.col -> parent.col`, one per line."""
        out = []
        for table, meta in self.tables.items():
            for fk in meta.get("foreign_keys", []):
                child, _, parent = fk.partition(" REFERENCES ")
                child_col = child.strip("()")
                parent = parent.split(".")[-1]
                parent_table, _, parent_col = parent.partition("(")
                out.append(
                    f"{table}.{child_col} -> {parent_table}.{parent_col.rstrip(')')}"
                )
        return "\n".join(sorted(out))

    def render_rules(self) -> str:
        return "\n".join(
            f"- {' '.join(rule['text'].split())}"
            for rule in self.raw["business_rules"]
        )

    def render_coverage(self) -> str:
        out = []
        for col, span in self.raw["data_coverage"].items():
            tail = "  (contains future-dated rows)" if span.get("forward_looking") else ""
            out.append(f"- {col}: {span['min'][:10]} to {span['max'][:10]}{tail}")
        return "\n".join(out)


def _short_type(pg_type: str) -> str:
    if pg_type.startswith("numeric"):
        return "num"
    return {
        "integer": "int",
        "text": "str",
        "boolean": "bool",
        "date": "date",
        "timestamp without time zone": "ts",
    }.get(pg_type, pg_type)


@functools.lru_cache(maxsize=1)
def load(path: pathlib.Path | None = None) -> Catalog:
    src = path or CATALOG_PATH
    return Catalog(yaml.safe_load(src.read_text(encoding="utf-8")))
