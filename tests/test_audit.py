"""Audit log tests. The eval harness reads these records, so the format is a
contract rather than debug output."""

from __future__ import annotations

import json

from server.audit import AuditLog


def test_records_round_trip(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.write({"conversation_id": "abc", "turn": 1, "action": "answer"})
    log.write({"conversation_id": "abc", "turn": 2, "action": "refuse"})

    records = log.read_all()
    assert [r["turn"] for r in records] == [1, 2]
    assert all("ts" in r for r in records)


def test_missing_file_reads_as_empty(tmp_path):
    assert AuditLog(str(tmp_path / "nothing.jsonl")).read_all() == []


def test_parent_directory_is_created(tmp_path):
    log = AuditLog(str(tmp_path / "nested" / "deep" / "audit.jsonl"))
    log.write({"x": 1})
    assert log.path.exists()


def test_non_serialisable_values_do_not_raise(tmp_path):
    class Odd:
        def __repr__(self) -> str:
            return "<odd>"

    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.write({"value": Odd()})
    assert "<odd>" in json.dumps(log.read_all()[0])
