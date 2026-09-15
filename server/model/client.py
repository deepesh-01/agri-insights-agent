"""Model access behind one interface, so the bake-off can swap implementations.

Every candidate in model/models.yaml is served by llama-server with identical
flags, and the evaluation harness re-runs against each without touching any
other module. `ModelClient` is what makes that swap a configuration change
rather than a code change.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    stop_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def json(self) -> dict[str, Any]:
        """Parse the completion as JSON, tolerating a fenced or padded reply.

        A grammar-constrained response is already clean JSON. The fallbacks
        exist for the bake-off: not every candidate model honours a schema, and
        a model that wraps its answer in a code fence should be scored on the
        SQL it wrote, not penalised for packaging.
        """
        text = self.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text[3:] else text[3:]
            text = text.removeprefix("json").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise
            return json.loads(text[start : end + 1])


class ModelError(Exception):
    pass


@dataclass
class LlamaCppClient:
    """Client for a llama.cpp `llama-server` OpenAI-compatible endpoint."""

    base_url: str
    model_id: str = "local"
    timeout: float = 180.0
    temperature: float = 0.0
    seed: int = 1337
    # Optional TranscriptLog. When set, every exchange is recorded verbatim so
    # a turn can be reproduced or a divergence between runs explained.
    transcript: Any | None = None
    # Set by the pipeline so transcript rows can be tied back to a turn.
    context: dict[str, Any] = field(default_factory=dict)
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def close(self) -> None:
        self._client.close()

    def _record(self, system, user, response, payload, started, *,
                usage=None, error=None) -> None:
        if self.transcript is None:
            return
        usage = usage or {}
        self.transcript.record_call(
            stage=self.context.get("stage", "unknown"),
            system=system,
            user=user,
            response=response,
            model=self.model_id,
            temperature=payload.get("temperature", self.temperature),
            seed=payload.get("seed", self.seed),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            conversation_id=self.context.get("conversation_id"),
            turn=self.context.get("turn"),
            attempt=self.context.get("attempt", 0),
            error=error,
        )

    def health(self) -> dict[str, Any]:
        try:
            response = self._client.get("/health")
            props = self._client.get("/props").json()
            return {
                "ok": response.status_code == 200,
                "model": props.get("model_path", "").split("/")[-1],
                "ctx": props.get("default_generation_settings", {}).get("n_ctx"),
            }
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 700,
        temperature: float | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature if temperature is None else temperature,
            "seed": self.seed,
            "max_tokens": max_tokens,
            "cache_prompt": True,
        }
        if json_schema is not None:
            # llama-server compiles this to a GBNF grammar internally, which is
            # what actually forces well-formed output. It is a syntax
            # guarantee only; the SQL validator still does the semantic work.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            }

        started = time.perf_counter()
        try:
            response = self._client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            # Failures are recorded too: a turn that degraded to a refusal
            # because the server returned 500 looks identical in the report to
            # one the model genuinely refused.
            self._record(system, user, "", payload, started, error=str(exc))
            raise ModelError(f"model request failed: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        choice = body["choices"][0]
        usage = body.get("usage", {})
        self._record(system, user, choice["message"]["content"] or "", payload,
                     started, usage=usage)
        return Completion(
            text=choice["message"]["content"] or "",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
            stop_reason=choice.get("finish_reason", ""),
        )
