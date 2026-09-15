"""FastAPI surface for the agent.

Deliberately small: the routes do argument handling and serialisation, and the
pipeline does the work. No streaming and no charting -- both are out of scope
for this assignment.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from server.audit import AuditLog
from server.catalog import load as load_catalog
from server.config import settings
from server.context.prompts import versions as prompt_versions
from server.conversation.state import ConversationStore
from server.model.client import LlamaCppClient
from server.pipeline import Pipeline
from server.sql.executor import Executor
from server.transcript import TranscriptLog

state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    catalog = load_catalog()
    model = LlamaCppClient(
        settings.llama_url,
        model_id=settings.model_id,
        transcript=TranscriptLog(settings.transcript_path),
    )
    executor = Executor(
        settings.agent_dsn, statement_timeout_ms=settings.statement_timeout_ms
    )
    store = ConversationStore(
        turn_window=settings.turn_window,
        slot_max_age_turns=settings.slot_max_age_turns,
    )
    state["catalog"] = catalog
    state["model"] = model
    state["executor"] = executor
    state["pipeline"] = Pipeline(
        catalog=catalog,
        model=model,
        executor=executor,
        store=store,
        settings=settings,
        audit=AuditLog(settings.audit_path),
    )
    state["store"] = store
    yield
    model.close()
    executor.close()


app = FastAPI(title="Agri Insights Agent", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None


@app.post("/api/ask")
def ask(request: AskRequest) -> dict[str, Any]:
    pipeline: Pipeline = state["pipeline"]
    return pipeline.ask(request.message, request.conversation_id).to_json()


@app.get("/api/schema")
def schema() -> dict[str, Any]:
    """Catalog and data coverage. The UI shows coverage before the first
    question, so the user learns the boundary before they hit it."""
    catalog = state["catalog"]
    return {
        "catalog_version": catalog.version,
        "schema": catalog.schema,
        "latest_observation_date": catalog.latest_observation_date,
        "data_coverage": catalog.raw["data_coverage"],
        "forward_looking_columns": catalog.raw["forward_looking_columns"],
        "tables": {
            name: {
                "rows": meta["rows"],
                "description": meta.get("description"),
                "columns": {
                    col: {
                        "type": cmeta["type"],
                        "nullable": cmeta["nullable"],
                        "nulls": cmeta.get("nulls"),
                        "values": list(cmeta.get("values", {})) or None,
                        "description": cmeta.get("description"),
                    }
                    for col, cmeta in meta["columns"].items()
                },
            }
            for name, meta in catalog.tables.items()
        },
        "business_rules": catalog.raw["business_rules"],
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    model_health = state["model"].health()
    try:
        state["executor"].run("SELECT 1 AS ok FROM farmer LIMIT 1", check_cost=False)
        db_ok = True
        db_error = None
    except Exception as exc:  # noqa: BLE001 - health must report, not raise
        db_ok, db_error = False, str(exc)

    return {
        "ok": bool(model_health.get("ok")) and db_ok,
        "model": {**model_health, "id": settings.model_id},
        "database": {"ok": db_ok, "error": db_error},
        "catalog_version": state["catalog"].version,
        "prompt_versions": prompt_versions(),
    }


@app.get("/api/conversation/{conversation_id}")
def conversation(conversation_id: str) -> dict[str, Any]:
    store: ConversationStore = state["store"]
    if conversation_id not in store._store:  # noqa: SLF001 - read-only accessor
        raise HTTPException(status_code=404, detail="unknown conversation")
    convo = store.get(conversation_id)
    return {"state": convo.to_json(), "turns": [t.to_json() for t in convo.turns]}


@app.post("/api/conversation/{conversation_id}/reset")
def reset(conversation_id: str) -> dict[str, str]:
    state["store"].reset(conversation_id)
    return {"status": "reset"}
