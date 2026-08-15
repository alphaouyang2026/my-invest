"""HTTP surface of the data-sync module (design §15.1)."""

from datetime import date, timedelta

import pytest

from tests.fakes import FakeAdapter

DATES = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(4)]


@pytest.fixture
def configured_key(monkeypatch):
    """The route refuses to queue work without a key; these tests are about
    the interface, not the credential check."""
    monkeypatch.setattr("app.api.data_sync.resolve_jquants_api_key", lambda: "test-key")


def test_sync_routes_are_published_in_openapi() -> None:
    from app.main import app

    paths = app.openapi()["paths"]

    assert "post" in paths["/api/v1/data-sync/jquants"]
    assert "get" in paths["/api/v1/data-sync/runs"]
    assert "get" in paths["/api/v1/data-sync/runs/{run_id}"]
    assert "post" in paths["/api/v1/data-sync/runs/{run_id}/cancel"]
    assert "post" in paths["/api/v1/data-sync/runs/{run_id}/resume"]


def test_sync_now_exposes_no_execution_parameters() -> None:
    """Batch size and date ranges are SyncPolicy, not user interface."""
    from app.main import app

    schema = app.openapi()["paths"]["/api/v1/data-sync/jquants"]["post"]
    reference = schema["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    properties = app.openapi()["components"]["schemas"][reference.split("/")[-1]].get("properties", {})

    assert properties == {}, f"sync now must take no parameters, got {sorted(properties)}"


def test_starting_a_sync_returns_the_run(client, configured_key) -> None:
    response = client.post("/api/v1/data-sync/jquants", json={})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["phase"] == "discovering_calendar"


def test_repeating_an_idempotency_key_returns_the_same_run(client, configured_key) -> None:
    first = client.post(
        "/api/v1/data-sync/jquants", json={}, headers={"Idempotency-Key": "same-key"}
    )
    second = client.post(
        "/api/v1/data-sync/jquants", json={}, headers={"Idempotency-Key": "same-key"}
    )

    assert first.json()["id"] == second.json()["id"]


def test_run_detail_reports_batches_and_publications(client, make_workflow) -> None:
    adapter = FakeAdapter(trading_dates=DATES)
    workflow = make_workflow(adapter, batch_size=2)
    run_id = workflow.start().id
    workflow.execute(run_id)

    response = client.get(f"/api/v1/data-sync/runs/{run_id}")

    assert response.status_code == 202 or response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["total_batches"] == 2
    assert len(body["batches"]) == 2
    endpoints = [item["endpoint"] for item in body["publications"]]
    assert endpoints.count("equities/bars/daily") == 2
    assert "markets/calendar" in endpoints


def test_unknown_run_is_reported_as_not_found(client) -> None:
    response = client.get("/api/v1/data-sync/runs/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


def test_resuming_a_queued_run_is_rejected(client, sync_workflow) -> None:
    run_id = sync_workflow.start().id

    response = client.post(f"/api/v1/data-sync/runs/{run_id}/resume")

    assert response.status_code == 409
