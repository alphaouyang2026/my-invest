"""Single-worker guarantees and crash recovery (acceptance §18.4)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models.market_data import (
    EndpointPublication,
    PublicationStatus,
    SyncRun,
    SyncRunStatus,
)
from app.models.task import Task, TaskStatus
from app.services.jquants_sync_workflow import SyncConflict, SyncPolicy
from app.worker.registry import get_handler, register
from app.worker.runner import acquire_singleton_lock, recover_orphaned_tasks
from tests.fakes import FakeAdapter

DATES = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(12)]


def _mark_running(session_factory, run_id, *, attempt_count: int = 1) -> None:
    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        run.status = SyncRunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        task = session.get(Task, run.task_id)
        task.status = TaskStatus.RUNNING
        task.attempt_count = attempt_count
        session.commit()


def test_a_second_worker_cannot_take_the_singleton_lock(engine):
    """§18.4 item 1: the second process must exit before polling."""
    first = acquire_singleton_lock(engine)
    assert first is not None
    try:
        assert acquire_singleton_lock(engine) is None
    finally:
        first.close()

    # Released with the connection, so the next worker starts cleanly.
    third = acquire_singleton_lock(engine)
    assert third is not None
    third.close()


def test_orphaned_sync_task_is_requeued_from_its_checkpoint(sync_workflow, session_factory):
    """§18.4 item 2 — the change from the old behaviour, which failed the run
    outright because no checkpoint existed to resume from."""
    run_id = sync_workflow.start().id
    _mark_running(session_factory, run_id)

    assert recover_orphaned_tasks(session_factory) == 1

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        assert run.status == SyncRunStatus.QUEUED
        assert run.finished_at is None
        task = session.get(Task, run.task_id)
        assert task.status == TaskStatus.QUEUED
        assert task.started_at is None


def test_recovery_fails_staging_publications_but_keeps_them_for_audit(
    make_workflow, session_factory
):
    adapter = FakeAdapter(trading_dates=DATES)
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id
    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        session.add(
            EndpointPublication(
                sync_run_id=run_id,
                created_by_task_id=run.task_id,
                created_by_task_attempt=1,
                endpoint="equities/bars/daily",
                scope_ordinal=0,
                attempt=1,
                status=PublicationStatus.STAGING,
                adapter_version="test",
            )
        )
        session.commit()
    _mark_running(session_factory, run_id)

    recover_orphaned_tasks(session_factory)

    with session_factory() as session:
        publication = session.scalar(
            select(EndpointPublication).where(EndpointPublication.sync_run_id == run_id)
        )
        assert publication.status == PublicationStatus.FAILED
        assert publication.error_code == "worker_restart"
        assert publication.publish_sequence is None


def test_terminal_run_with_a_running_task_is_repaired_not_requeued(sync_workflow, session_factory):
    """§18.4 item 7: legacy inconsistency — the run already decided, so only
    the task is corrected."""
    run_id = sync_workflow.start().id
    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        run.status = SyncRunStatus.SUCCEEDED
        run.finished_at = datetime.now(timezone.utc)
        session.get(Task, run.task_id).status = TaskStatus.RUNNING
        session.commit()

    recover_orphaned_tasks(session_factory)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        assert run.status == SyncRunStatus.SUCCEEDED
        task = session.get(Task, run.task_id)
        assert task.status == TaskStatus.SUCCEEDED
        assert task.finished_at is not None


def test_exhausted_attempts_terminate_the_run(sync_workflow, session_factory):
    run_id = sync_workflow.start().id
    _mark_running(session_factory, run_id, attempt_count=SyncPolicy().max_task_attempts)

    recover_orphaned_tasks(session_factory)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        assert run.status == SyncRunStatus.FAILED
        assert run.error_code == "worker_restart"
        assert session.get(Task, run.task_id).status == TaskStatus.FAILED


def test_cancelling_run_completes_its_cancellation_on_restart(sync_workflow, session_factory):
    run_id = sync_workflow.start().id
    _mark_running(session_factory, run_id)
    sync_workflow.request_cancel(run_id)

    recover_orphaned_tasks(session_factory)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        assert run.status == SyncRunStatus.CANCELLED
        assert session.get(Task, run.task_id).status == TaskStatus.CANCELLED


def test_generic_orphan_task_without_a_sync_run_is_failed(session_factory):
    with session_factory() as session:
        task = Task(task_type="noop", status=TaskStatus.RUNNING, started_at=datetime.now(timezone.utc))
        session.add(task)
        session.commit()
        task_id = task.id

    assert recover_orphaned_tasks(session_factory) == 1

    with session_factory() as session:
        reloaded = session.get(Task, task_id)
        assert reloaded.status == TaskStatus.FAILED
        assert reloaded.error is not None


def test_only_one_active_run_exists_per_source(sync_workflow, session_factory):
    """§18.4 item 4."""
    first = sync_workflow.start()
    second = sync_workflow.start()

    assert first.id == second.id
    with session_factory() as session:
        active = session.scalar(
            select(func.count())
            .select_from(SyncRun)
            .where(SyncRun.status.in_([SyncRunStatus.QUEUED, SyncRunStatus.RUNNING]))
        )
        assert active == 1


def test_a_second_resume_does_not_queue_the_task_twice(make_workflow, session_factory):
    """§18.4 item 3: the second caller sees the task is already queued."""
    adapter = FakeAdapter(trading_dates=DATES, fail_dates={DATES[6]})
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id
    with pytest.raises(Exception):
        workflow.execute(run_id)

    workflow.resume(run_id)
    with pytest.raises(SyncConflict):
        workflow.resume(run_id)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        assert run.status == SyncRunStatus.QUEUED
        assert session.get(Task, run.task_id).status == TaskStatus.QUEUED


def test_the_same_idempotency_key_returns_the_same_run(sync_workflow, make_workflow):
    """§18.4 item 5 — still true after the first run reached a terminal state."""
    first = sync_workflow.start(idempotency_key="abc")
    adapter = FakeAdapter(trading_dates=DATES[:2])
    make_workflow(adapter, batch_size=5).execute(first.id)

    repeat = sync_workflow.start(idempotency_key="abc")

    assert repeat.id == first.id
    assert repeat.status == SyncRunStatus.SUCCEEDED


def test_successful_run_terminates_run_and_task_together(make_workflow, session_factory):
    """§18.4 item 6: "succeeded run with a RUNNING task" is never committed."""
    adapter = FakeAdapter(trading_dates=DATES[:2])
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id

    workflow.execute(run_id)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        task = session.get(Task, run.task_id)
        assert run.status == SyncRunStatus.SUCCEEDED
        assert task.status == TaskStatus.SUCCEEDED
        assert run.finished_at is not None and task.finished_at is not None


def test_registered_handler_is_retrievable():
    calls = []

    @register("smoke_test_task")
    def handler(payload: dict) -> dict:
        calls.append(payload)
        return {"ok": True}

    assert get_handler("smoke_test_task") is handler
    handler({"x": 1})
    assert calls == [{"x": 1}]
