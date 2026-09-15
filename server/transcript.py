"""Full transcript of every model call.

The evaluation reports record what the agent *did* — the SQL it produced, the
attempts it made, the answer it gave. They do not record what the model was
actually asked or what it literally replied, which makes two things impossible:
reproducing a specific turn outside the harness, and explaining why two runs
with the same seed diverged.

This records the raw exchange for every call: rendered prompts, raw response
text, sampling parameters, tokens and latency.

System prompts are ~3,500 tokens and identical across calls of the same stage,
so they are stored once under a hash and referenced thereafter. A 55-turn run
is roughly 165 calls; verbatim storage would be ~2 MB of near-duplicate text,
and deduplication takes it to a fraction of that without losing anything.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_lock = threading.Lock()


@dataclass
class TranscriptLog:
    """Append-only JSONL of raw model exchanges."""

    path: pathlib.Path
    _seen_prompts: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.path = pathlib.Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str, ensure_ascii=False)
        with _lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _prompt_ref(self, text: str, kind: str) -> str:
        """Store a long prompt once; return a hash reference for later calls."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if digest not in self._seen_prompts:
            self._seen_prompts.add(digest)
            self._write({
                "type": "prompt_body",
                "hash": digest,
                "kind": kind,
                "chars": len(text),
                "text": text,
            })
        return digest

    def record_call(
        self,
        *,
        stage: str,
        system: str,
        user: str,
        response: str,
        model: str,
        temperature: float,
        seed: int,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        conversation_id: str | None = None,
        turn: int | None = None,
        attempt: int = 0,
        error: str | None = None,
    ) -> None:
        self._write({
            "type": "call",
            "ts": time.time(),
            "conversation_id": conversation_id,
            "turn": turn,
            "stage": stage,              # classify | generate | narrate
            "attempt": attempt,
            "model": model,
            "temperature": temperature,
            "seed": seed,
            # The system prompt repeats every call; the user prompt is the part
            # that actually varies, so it is stored in full every time.
            "system_hash": self._prompt_ref(system, f"{stage}:system"),
            "user": user,
            "response": response,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
            "error": error,
        })

    # ----------------------------------------------------------------- read --
    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def calls(self) -> list[dict[str, Any]]:
        return [r for r in self.read() if r["type"] == "call"]

    def resolve(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return a call with its system prompt text substituted back in."""
        bodies = {r["hash"]: r["text"] for r in self.read() if r["type"] == "prompt_body"}
        return {**record, "system": bodies.get(record.get("system_hash"), "")}
