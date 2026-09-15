"""Append-only audit log.

Every turn is written as one JSON line: the question, the intent, each SQL
attempt with its verdict, timings and token counts, and the prompt versions
that produced it. This is not observability decoration -- the evaluation
harness reads the same records, so a number in the report can always be traced
back to the exact prompt text and model that produced it.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from typing import Any

_lock = threading.Lock()


class AuditLog:
    def __init__(self, path: str) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        record = {"ts": time.time(), **record}
        line = json.dumps(record, default=str, ensure_ascii=False)
        with _lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
