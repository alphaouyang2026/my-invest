"""The snapshot and findings HTTP surface."""

from __future__ import annotations

from datetime import date, timedelta

from tests.fakes import FakeAdapter, bar_row

OPEN_DAYS = [date(2024, 3, 4) + timedelta(days=offset) for offset in range(5)]


def _sync(sync_workflow, adapter):
    run_id = sync_workflow.start().id
    sync_workflow._adapter = adapter
    sync_workflow.execute(run_id)
    return run_id


def test_a_clean_snapshot_is_listed_as_current_and_usable(client, sync_workflow):
    _sync(sync_workflow, FakeAdapter(trading_dates=OPEN_DAYS))

    listed = client.get("/api/v1/snapshots").json()

    assert len(listed) == 1
    assert listed[0]["version"] == 1
    assert listed[0]["is_backtest_eligible"] is True
    assert listed[0]["is_current"] is True


def test_the_detail_view_carries_the_reasons_a_snapshot_was_rejected(client, sync_workflow):
    """A verdict without the rule and the date behind it is not traceable."""

    def bars(trade_date: date) -> list[dict]:
        return [bar_row("13010", trade_date, close=-5), bar_row("13020", trade_date)]

    _sync(sync_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bars))
    snapshot_id = client.get("/api/v1/snapshots").json()[0]["id"]

    detail = client.get(f"/api/v1/snapshots/{snapshot_id}").json()

    assert detail["is_backtest_eligible"] is False
    rejecting = [item for item in detail["findings"] if item["severity"] == "rejecting"]
    assert rejecting
    assert rejecting[0]["rule"] == "negative_price"
    assert rejecting[0]["affected_count"] == 1
    assert rejecting[0]["evaluated_count"] == 2
    assert rejecting[0]["sample"] == ["13010"]


def test_a_revalidated_snapshot_carries_its_own_findings_and_says_where_it_came_from(
    client, sync_workflow, revalidation_workflow
):
    """Two snapshots with identical coverage are otherwise indistinguishable,
    and each must answer with the verdict its own evaluation reached."""

    def bars(trade_date: date) -> list[dict]:
        return [bar_row("13010", trade_date, close=-5), bar_row("13020", trade_date)]

    _sync(sync_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bars))
    revalidation_workflow.execute(revalidation_workflow.start().id)

    listed = client.get("/api/v1/snapshots").json()
    latest = [item for item in listed if item["is_current"]][0]
    detail = client.get(f"/api/v1/snapshots/{latest['id']}").json()

    assert [item["evaluation_kind"] for item in listed] == ["revalidate", "sync"]
    assert latest["sync_run_id"] is None
    assert detail["is_backtest_eligible"] is False
    rejecting = [item for item in detail["findings"] if item["severity"] == "rejecting"]
    assert {item["rule"] for item in rejecting} == {"negative_price"}
    assert {item["trade_date"] for item in rejecting} == {day.isoformat() for day in OPEN_DAYS}


def test_an_unknown_snapshot_is_a_404(client):
    response = client.get("/api/v1/snapshots/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
