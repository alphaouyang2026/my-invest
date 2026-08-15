"""Cooperative cancellation (design §13.2, acceptance §18.5)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models.market_data import (
    CurrentBar,
    DataSnapshot,
    EndpointPublication,
    PublicationStatus,
    SyncBatch,
    SyncBatchStatus,
    SyncRun,
    SyncRunStatus,
)
from app.models.task import Task, TaskStatus
from app.services.jquants_sync_workflow import SyncCancelled, SyncConflict
from tests.fakes import FakeAdapter, bar_row

DATES = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(12)]
CODE = "13010"


def test_cancelling_a_queued_run_terminates_both_records_atomically(sync_workflow, session_factory):
    """§18.5 item 5: nothing has been claimed, so there is nothing to unwind."""
    run_id = sync_workflow.start().id

    view = sync_workflow.request_cancel(run_id)

    assert view.status == SyncRunStatus.CANCELLED
    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        task = session.get(Task, run.task_id)
        assert task.status == TaskStatus.CANCELLED
        assert run.finished_at is not None and task.finished_at is not None


def test_cancelling_a_running_run_keeps_the_task_running(sync_workflow, session_factory):
    """§18.5 item 6: the worker still has to finish its HTTP response and
    clean up staging state, so only the run moves to CANCELLING."""
    run_id = sync_workflow.start().id
    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        run.status = SyncRunStatus.RUNNING
        session.get(Task, run.task_id).status = TaskStatus.RUNNING
        session.commit()

    view = sync_workflow.request_cancel(run_id)

    assert view.status == SyncRunStatus.CANCELLING
    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        assert session.get(Task, run.task_id).status == TaskStatus.RUNNING
        assert run.cancel_requested_at is not None


def test_cancelling_mid_run_keeps_published_batches_and_creates_no_snapshot(
    make_workflow, session_factory
):
    """§18.5 items 1 and 8: the batch that already committed stays official;
    work after the safe point stops."""
    holder: dict = {}

    def bars(trade_date: date):
        if trade_date == DATES[6]:
            holder["workflow"].request_cancel(holder["run_id"])
        return [bar_row(CODE, trade_date)]

    adapter = FakeAdapter(trading_dates=DATES, bars=bars)
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id
    holder.update(workflow=workflow, run_id=run_id)

    with pytest.raises(SyncCancelled):
        workflow.execute(run_id)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        assert run.status == SyncRunStatus.CANCELLED
        assert session.get(Task, run.task_id).status == TaskStatus.CANCELLED

        batches = session.scalars(
            select(SyncBatch).where(SyncBatch.sync_run_id == run_id).order_by(SyncBatch.ordinal)
        ).all()
        assert batches[0].status == SyncBatchStatus.PUBLISHED
        assert batches[1].status == SyncBatchStatus.CANCELLED
        assert batches[2].status == SyncBatchStatus.CANCELLED

        assert session.scalar(select(func.count()).select_from(DataSnapshot)) == 0
        assert adapter.master_requests == 0

        # §18.5 item 7: the generation in flight when cancel committed must
        # not publish, and must not touch the current-bar projection.
        cancelled = session.scalars(
            select(EndpointPublication).where(
                EndpointPublication.sync_run_id == run_id,
                EndpointPublication.status == PublicationStatus.CANCELLED,
            )
        ).all()
        assert cancelled, "the in-flight bars publication should end cancelled"
        assert all(item.publish_sequence is None for item in cancelled)
        assert session.scalar(select(func.count()).select_from(CurrentBar)) == 5


def test_a_cancelled_run_resumes_from_its_frozen_plan(make_workflow, session_factory):
    """§18.5 item 3."""
    holder: dict = {}

    def bars(trade_date: date):
        if trade_date == DATES[6] and "cancelled" not in holder:
            holder["cancelled"] = True
            holder["workflow"].request_cancel(holder["run_id"])
        return [bar_row(CODE, trade_date)]

    adapter = FakeAdapter(trading_dates=DATES, bars=bars)
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id
    holder.update(workflow=workflow, run_id=run_id)
    with pytest.raises(SyncCancelled):
        workflow.execute(run_id)

    workflow.resume(run_id)
    outcome = workflow.execute(run_id)

    assert outcome.status == SyncRunStatus.SUCCEEDED
    assert adapter.calendar_requests == 1, "the frozen plan must be reused"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DataSnapshot)) == 1


def test_cancelling_a_terminal_run_is_rejected(make_workflow, sync_workflow):
    adapter = FakeAdapter(trading_dates=DATES[:2])
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id
    workflow.execute(run_id)

    with pytest.raises(SyncConflict):
        workflow.request_cancel(run_id)


def test_cancel_endpoint_reports_conflict_for_a_terminal_run(client, make_workflow):
    adapter = FakeAdapter(trading_dates=DATES[:2])
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id
    workflow.execute(run_id)

    response = client.post(f"/api/v1/data-sync/runs/{run_id}/cancel")

    assert response.status_code == 409


def test_cancel_endpoint_cancels_a_queued_run(client, sync_workflow):
    run_id = sync_workflow.start().id

    response = client.post(f"/api/v1/data-sync/runs/{run_id}/cancel")

    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"
