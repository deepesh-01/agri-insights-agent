"""Check every gold_sql in the evaluation set runs and returns sensible rows.

A gold query that errors, or that silently returns nothing because it fell into
one of the data traps, would score the agent's correct answer as wrong. The
harness is an instrument; this is its calibration check.
"""

from __future__ import annotations

import pathlib
import sys

import yaml

from server.config import settings
from server.sql.executor import Executor, ExecutionError

CONV_DIR = pathlib.Path("eval/conversations")


def main() -> int:
    executor = Executor(settings.agent_dsn)
    failures: list[str] = []
    empty: list[str] = []
    checked = 0
    turns_total = 0

    for path in sorted(CONV_DIR.glob("*.yaml")):
        conversation = yaml.safe_load(path.read_text())
        for index, turn in enumerate(conversation["turns"], start=1):
            turns_total += 1
            sql = turn.get("gold_sql")
            expect = turn["expect"]

            if expect != "answer":
                if sql:
                    failures.append(
                        f"{conversation['id']} turn {index}: expect={expect} "
                        "must not carry gold_sql"
                    )
                continue
            if not sql:
                failures.append(
                    f"{conversation['id']} turn {index}: expect=answer needs gold_sql"
                )
                continue

            checked += 1
            try:
                result = executor.run(sql)
            except ExecutionError as exc:
                failures.append(
                    f"{conversation['id']} turn {index}: {exc.code}: {exc.message}"
                )
                continue

            marker = "  <- EMPTY" if result.row_count == 0 else ""
            if result.row_count == 0:
                empty.append(f"{conversation['id']} turn {index}")
            print(
                f"{conversation['id']:28} turn {index}  "
                f"{result.row_count:>5} rows  {result.duration_ms:>4} ms{marker}"
            )

    executor.close()
    print(f"\n{len(list(CONV_DIR.glob('*.yaml')))} conversations, "
          f"{turns_total} turns, {checked} gold queries executed")

    if empty:
        print(f"\nEmpty results ({len(empty)}) - intentional only where the "
              f"conversation says so:")
        for item in empty:
            print(f"  {item}")

    if failures:
        print(f"\nFAILURES ({len(failures)}):", file=sys.stderr)
        for item in failures:
            print(f"  {item}", file=sys.stderr)
        return 1
    print("\nall gold SQL valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
