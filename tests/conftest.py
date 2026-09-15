"""Shared fixtures.

The pipeline tests run against a scripted fake model rather than llama-server.
That is deliberate: retry logic, refusal handling and state transitions are
deterministic control flow, and testing them through a 7B model would make the
suite slow, flaky and unable to reproduce the exact failure sequences that
matter (a bad column, then a timeout, then success).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.catalog import load as load_catalog
from server.config import Settings
from server.conversation.state import ConversationStore
from server.model.client import Completion
from server.pipeline import Pipeline
from server.sql.executor import Executor


@dataclass
class FakeModel:
    """Returns scripted responses in order, recording what it was asked."""

    responses: list[Any] = field(default_factory=list)
    model_id: str = "fake"
    calls: list[dict[str, str]] = field(default_factory=list)
    _index: int = 0

    def complete(self, system: str, user: str, **kwargs) -> Completion:
        self.calls.append({
            "system": system,
            "user": user,
            "temperature": kwargs.get("temperature"),
        })
        if self._index >= len(self.responses):
            raise AssertionError(
                f"FakeModel ran out of responses after {self._index} calls"
            )
        payload = self.responses[self._index]
        self._index += 1
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return Completion(
            text=text, prompt_tokens=100, completion_tokens=20, latency_ms=1
        )

    def close(self) -> None:
        pass

    def health(self) -> dict[str, Any]:
        return {"ok": True, "model": "fake.gguf", "ctx": 8192}

    @property
    def last_user_prompt(self) -> str:
        return self.calls[-1]["user"] if self.calls else ""


@pytest.fixture(scope="session")
def catalog():
    return load_catalog()


@pytest.fixture(scope="session")
def settings():
    return Settings()


@pytest.fixture
def executor(settings):
    ex = Executor(settings.agent_dsn)
    yield ex
    ex.close()


@pytest.fixture
def make_pipeline(catalog, executor, settings):
    """Build a pipeline around a scripted model."""

    def build(responses: list[Any], **overrides) -> tuple[Pipeline, FakeModel]:
        from dataclasses import replace

        model = FakeModel(responses=list(responses))
        pipeline = Pipeline(
            catalog=catalog,
            model=model,
            executor=executor,
            store=ConversationStore(
                turn_window=settings.turn_window,
                slot_max_age_turns=settings.slot_max_age_turns,
            ),
            settings=replace(settings, **overrides) if overrides else settings,
            audit=None,
        )
        return pipeline, model

    return build


NARRATION = "Plain English answer."
