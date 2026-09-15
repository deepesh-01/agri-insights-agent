"""API surface tests.

The routes are exercised against a scripted model and the real database, with
the app's lifespan state populated directly -- starting llama-server inside the
unit suite would make it slow and machine-dependent for no extra coverage of
the routing logic itself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import app as app_module
from server.catalog import load as load_catalog
from server.config import Settings
from server.conversation.state import ConversationStore
from server.pipeline import Pipeline
from server.sql.executor import Executor
from tests.conftest import FakeModel

COUNT_SQL = "SELECT count(*) AS n FROM farmer WHERE district = 'Belgaum'"


@pytest.fixture
def client():
    settings = Settings()
    catalog = load_catalog()
    model = FakeModel(responses=[
        {"action": "sql", "sql": COUNT_SQL, "assumptions": []},
        "There are 26 farmers in Belgaum.",
    ])
    executor = Executor(settings.agent_dsn)
    store = ConversationStore()

    injected = {
        "catalog": catalog,
        "model": model,
        "executor": executor,
        "store": store,
        "pipeline": Pipeline(
            catalog=catalog, model=model, executor=executor,
            store=store, settings=settings, audit=None,
        ),
    }
    with TestClient(app_module.app, raise_server_exceptions=True) as test_client:
        # The lifespan has just built a real llama.cpp client and its own
        # pipeline; replace them now so the routes run against the scripted
        # model instead of a server that is not running during unit tests.
        app_module.state.update(injected)
        yield test_client
    executor.close()


def test_ask_returns_a_full_envelope(client):
    response = client.post("/api/ask", json={"message": "how many farmers in Belgaum?"})
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "answer"
    assert body["rows"] == [[26]]
    assert body["sql"]
    assert "state" in body and "timings_ms" in body


def test_ask_rejects_an_empty_message(client):
    assert client.post("/api/ask", json={"message": ""}).status_code == 422


def test_schema_exposes_coverage_before_any_question(client):
    body = client.get("/api/schema").json()
    assert body["latest_observation_date"]
    assert "sensor_reading.recorded_at" in body["data_coverage"]
    assert body["forward_looking_columns"] == ["crop_cycle.sown_date"]
    assert set(body["tables"]) == {
        "farmer", "plot", "crop_cycle", "sensor_reading",
        "advisory", "field_visit", "field_agent",
    }


def test_schema_includes_the_business_rules(client):
    rules = client.get("/api/schema").json()["business_rules"]
    assert any(r["id"] == "yield_units" for r in rules)
    assert any(r["id"] == "sensor_sentinels" for r in rules)


def test_health_reports_database_and_model(client):
    body = client.get("/api/health").json()
    assert body["database"]["ok"] is True
    assert "prompt_versions" in body
    assert body["catalog_version"] >= 1


def test_conversation_is_retrievable_then_resettable(client):
    conversation_id = client.post(
        "/api/ask", json={"message": "how many farmers in Belgaum?"}
    ).json()["conversation_id"]

    fetched = client.get(f"/api/conversation/{conversation_id}").json()
    assert fetched["state"]["conversation_id"] == conversation_id
    assert len(fetched["turns"]) == 1

    assert client.post(f"/api/conversation/{conversation_id}/reset").status_code == 200
    assert client.get(f"/api/conversation/{conversation_id}").status_code == 404


def test_unknown_conversation_is_404(client):
    assert client.get("/api/conversation/nope").status_code == 404
