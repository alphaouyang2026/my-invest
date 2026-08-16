"""Single-worker task runner (docs/design/jquants-continuous-batch-sync.md §6.2, §13.4).

One process, one worker, serial execution — no Redis, no Kafka, no lease or
heartbeat protocol. The singleton advisory lock below is misconfiguration
protection, *not* a takeover protocol: a second worker exits rather than
competing for tasks.
"""

import hashlib
import time
from datetime import datetime, timezone

from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.core.logging import get_logger
from app.models.market_data import (
    TERMINAL_RUN_STATUSES,
    EndpointPublication,
    PublicationStatus,
    QualityEvaluation,
    QualityEvaluationStatus,
    SyncRun,
    SyncRunStatus,
)
from app.models.task import Task, TaskStatus
from app.services.jquants_sync_workflow import SyncCancelled, SyncPolicy
from app.worker.registry import get_handler

logger = get_logger(__name__)

POLL_INTERVAL_SECONDS = 3
SINGLETON_LOCK_NAME = "task-runner:singleton"

_RUN_TERMINAL_TO_TASK = {
    SyncRunStatus.SUCCEEDED: TaskStatus.SUCCEEDED,
    SyncRunStatus.NO_CHANGE: TaskStatus.SUCCEEDED,
    SyncRunStatus.FAILED: TaskStatus.FAILED,
    SyncRunStatus.PARTIAL_FAILED: TaskStatus.FAILED,
    SyncRunStatus.CANCELLED: TaskStatus.CANCELLED,
}


def _advisory_key(name: str) -> int:
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def acquire_singleton_lock(engine: Engine) -> Connection | None:
    """Session-level lock held for the worker's lifetime.

    Returns None if another worker already holds it — the caller must exit
    without polling. The lock releases automatically when this connection
    drops, so a crashed worker never blocks its replacement.
    """
    connection = engine.connect()
    # Detach so this connection is never returned to the pool: close() then
    # really drops the socket, which is what the design means by "the lock is
    # released when the connection drops".
    #
    # Without it the lock leaks onto an idle pooled connection, and the
    # failure is worse than a stuck lock — session-level advisory locks are
    # reentrant, so if the pool later hands that same session back,
    # pg_try_advisory_lock returns true and a second worker is wrongly told it
    # may run. A singleton guard must never fail open.
    connection.detach()
    acquired = connection.execute(
        select(func.pg_try_advisory_lock(_advisory_key(SINGLETON_LOCK_NAME)))
    ).scalar()
    if not acquired:
        connection.close()
        return None
    return connection


def recover_orphaned_tasks(
    session_factory: sessionmaker[Session], *, policy: SyncPolicy = SyncPolicy()
) -> int:
    """Reconcile tasks left RUNNING by a dead worker.

    The singleton lock has already proven no other worker is alive, so every
    RUNNING task here is an orphan. Sync runs are re-queued against their
    persisted checkpoint rather than failed outright — the batch/publication
    records are exactly the progress that survives a crash.
    """
    recovered = 0
    with session_factory() as session:
        orphans = session.scalars(select(Task).where(Task.status == TaskStatus.RUNNING)).all()
        for task in orphans:
            bind_contextvars(task_id=str(task.id))
            run = session.scalar(select(SyncRun).where(SyncRun.task_id == task.id).with_for_update())

            if run is None:
                # A generic task with no business checkpoint: nothing safe to resume.
                task.status = TaskStatus.FAILED
                task.error = "Orphaned by worker restart while RUNNING"
                task.finished_at = datetime.now(timezone.utc)
                _fail_orphaned_evaluation(session, task)
                logger.warning("task.orphaned_on_restart", task_type=task.task_type)
                recovered += 1
                continue

            recovered += _recover_sync_run(session, run, task, policy)

        session.commit()
        clear_contextvars()
    return recovered


def _fail_orphaned_evaluation(session: Session, task: Task) -> None:
    """Close out a re-validation whose worker died mid-pass.

    Nothing here is resumable — the pass is one set of aggregate queries, so it
    simply runs again — but the row must not be left RUNNING: it is what the UI
    reports, and what the next re-validation checks before starting. A stuck
    RUNNING row would be exactly the dead end this ticket exists to remove.
    """
    evaluation = session.scalar(
        select(QualityEvaluation).where(QualityEvaluation.task_id == task.id).with_for_update()
    )
    if evaluation is None or evaluation.status not in {
        QualityEvaluationStatus.QUEUED,
        QualityEvaluationStatus.RUNNING,
    }:
        return
    evaluation.status = QualityEvaluationStatus.FAILED
    evaluation.error_summary = "Orphaned by worker restart while running"
    logger.warning("quality_evaluation.orphaned_on_restart", evaluation_id=str(evaluation.id))


def _recover_sync_run(session: Session, run: SyncRun, task: Task, policy: SyncPolicy) -> int:
    now = datetime.now(timezone.utc)

    if run.status in TERMINAL_RUN_STATUSES:
        # Legacy inconsistency: a terminal run must never be re-queued. Repair
        # the task to match what the run already decided.
        task.status = _RUN_TERMINAL_TO_TASK[run.status]
        task.finished_at = task.finished_at or run.finished_at or now
        logger.warning("task.repaired_against_terminal_run", run_status=run.status.value)
        return 1

    session.execute(
        EndpointPublication.__table__.update()
        .where(
            EndpointPublication.sync_run_id == run.id,
            EndpointPublication.status == PublicationStatus.STAGING,
        )
        .values(status=PublicationStatus.FAILED, error_code="worker_restart")
    )

    if run.status == SyncRunStatus.CANCELLING:
        run.status = SyncRunStatus.CANCELLED
        run.finished_at = now
        task.status = TaskStatus.CANCELLED
        task.finished_at = now
        logger.info("sync_run.cancel_completed_on_restart")
        return 1

    if task.attempt_count >= policy.max_task_attempts:
        published = session.scalar(
            select(func.count())
            .select_from(EndpointPublication)
            .where(
                EndpointPublication.sync_run_id == run.id,
                EndpointPublication.status == PublicationStatus.PUBLISHED,
            )
        )
        run.status = SyncRunStatus.PARTIAL_FAILED if published else SyncRunStatus.FAILED
        run.error_code = "worker_restart"
        run.error_summary = f"Exceeded {policy.max_task_attempts} worker attempts"
        run.finished_at = now
        task.status = TaskStatus.FAILED
        task.error = run.error_summary
        task.finished_at = now
        logger.warning("sync_run.attempts_exhausted", attempts=task.attempt_count)
        return 1

    run.status = SyncRunStatus.QUEUED
    task.status = TaskStatus.QUEUED
    task.started_at = None
    task.finished_at = None
    logger.info("sync_run.requeued_from_checkpoint", attempt=task.attempt_count)
    return 1


def _claim_next_task(session: Session) -> Task | None:
    task = session.scalars(
        select(Task).where(Task.status == TaskStatus.QUEUED).order_by(Task.created_at).limit(1)
    ).first()
    if task is None:
        return None
    task.status = TaskStatus.RUNNING
    task.attempt_count += 1
    task.started_at = datetime.now(timezone.utc)
    run = session.scalar(select(SyncRun).where(SyncRun.task_id == task.id))
    if run is not None and run.status == SyncRunStatus.QUEUED:
        run.status = SyncRunStatus.RUNNING
    session.commit()
    return task


def _run_task(session: Session, task: Task) -> None:
    """Execute one task to completion before looking at the queue again.

    Handlers that own their own terminal state (the J-Quants workflow
    terminates run and task in one transaction) are left alone: this only
    writes a terminal status if the handler did not already commit one.
    """
    bind_contextvars(task_id=str(task.id), task_type=task.task_type)
    handler = get_handler(task.task_type)
    try:
        if handler is None:
            raise ValueError(f"No handler registered for task_type={task.task_type!r}")
        result = handler(task.payload) or {}
        session.refresh(task)
        if task.status == TaskStatus.RUNNING:
            task.progress = result
            task.status = TaskStatus.SUCCEEDED
            task.finished_at = datetime.now(timezone.utc)
        logger.info("task.succeeded")
    except SyncCancelled:
        session.rollback()
        session.refresh(task)
        if task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.CANCELLED
            task.finished_at = datetime.now(timezone.utc)
        logger.info("task.cancelled")
    except Exception as exc:  # noqa: BLE001 — task failures must never crash the worker loop
        session.rollback()
        session.refresh(task)
        if task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            task.finished_at = datetime.now(timezone.utc)
        logger.exception("task.failed")
    finally:
        session.commit()
        clear_contextvars()


def run_forever(session_factory: sessionmaker[Session], engine: Engine) -> None:
    """Fixed startup order: handlers are registered by the caller, then the
    singleton lock, then orphan recovery, then serial polling."""
    lock = acquire_singleton_lock(engine)
    if lock is None:
        logger.error("worker.singleton_lock_unavailable")
        raise SystemExit("Another task runner already holds the singleton lock")

    try:
        recovered = recover_orphaned_tasks(session_factory)
        if recovered:
            logger.info("worker.recovered_orphaned_tasks", count=recovered)
        logger.info("worker.started", poll_interval_seconds=POLL_INTERVAL_SECONDS)

        while True:
            with session_factory() as session:
                task = _claim_next_task(session)
                if task is not None:
                    _run_task(session, task)
            if task is None:
                time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        lock.close()
