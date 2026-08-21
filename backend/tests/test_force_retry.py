"""Force retry: the way out of an exhausted run.

Attempts are consumed by anything that stops the worker, a graceful restart
included, so a run can reach the cap without anything being wrong with the
data. Before this existed the only escape was editing the database by hand.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.market_data import SyncRun, SyncRunStatus
from app.models.task import Task, TaskStatus
from app.services.jquants_sync_workflow import SyncConflict
from tests.fakes import FakeAdapter

DATES = [date(2024, 3, 4) + timedelta(days=offset) for offset in range(5)]


def _exhausted(session_factory, run_id, status=SyncRunStatus.PARTIAL_FAILED):
    """Put a run where the worker leaves it after burning every attempt."""
    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        run.status = status
        run.error_code = "worker_restart"
        run.error_summary = "Exceeded 3 worker attempts"
        task = session.get(Task, run.task_id)
        task.status = TaskStatus.FAILED
        task.attempt_count = 3
        session.commit()


def test_force_retry_revives_a_run_whose_attempts_are_spent(make_workflow, session_factory):
    workflow = make_workflow(FakeAdapter(trading_dates=DATES))
    run_id = workflow.start().id
    _exhausted(session_factory, run_id)

    workflow.force_retry(run_id)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        task = session.get(Task, run.task_id)

    assert run.status is SyncRunStatus.QUEUED
    assert task.status is TaskStatus.QUEUED
    assert task.attempt_count == 0
    # Both halves in one transaction: a run left terminal with a cleared
    # counter is a state nothing else in the system is written against.
    assert run.error_code is None
    assert run.finished_at is None


def test_force_retry_also_revives_a_cancelled_run(make_workflow, session_factory):
    workflow = make_workflow(FakeAdapter(trading_dates=DATES))
    run_id = workflow.start().id
    _exhausted(session_factory, run_id, status=SyncRunStatus.CANCELLED)

    workflow.force_retry(run_id)

    with session_factory() as session:
        assert session.get(SyncRun, run_id).status is SyncRunStatus.QUEUED


def test_force_retry_refuses_a_successful_run(make_workflow, session_factory):
    """Re-judging finished data is re-validation's job, not this one."""
    workflow = make_workflow(FakeAdapter(trading_dates=DATES))
    run_id = workflow.start().id
    workflow.execute(run_id)

    with pytest.raises(SyncConflict):
        workflow.force_retry(run_id)


def test_force_retry_refuses_a_run_that_is_still_going(make_workflow, session_factory):
    workflow = make_workflow(FakeAdapter(trading_dates=DATES))
    run_id = workflow.start().id

    with pytest.raises(SyncConflict):
        workflow.force_retry(run_id)


def test_the_api_exposes_force_retry(client, sync_workflow, session_factory):
    run_id = sync_workflow.start().id
    _exhausted(session_factory, run_id)

    response = client.post(f"/api/v1/data-sync/runs/{run_id}/force-retry")

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
