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

from app.core.config import get_settings
from app.core.logging import get_logger
from app.research.publication import recover_publications
from app.models.task import Task, TaskStatus
from app.services.jquants_sync_workflow import SyncCancelled
from app.worker.registry import get_handler, get_recovery

logger = get_logger(__name__)

POLL_INTERVAL_SECONDS = 3
SINGLETON_LOCK_NAME = "task-runner:singleton"

ORPHAN_ERROR = "Orphaned by worker restart while RUNNING"


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


def recover_orphaned_tasks(session_factory: sessionmaker[Session]) -> int:
    """Reconcile tasks left RUNNING by a dead worker.

    The singleton lock has already proven no other worker is alive, so every
    RUNNING task here is an orphan. Each is failed first and then handed to the
    recovery registered for its type, which owns the business row and may put
    the task back on the queue if it has a checkpoint worth resuming.

    A type with no recovery registered is reported rather than quietly failed.
    The runner cannot tell "nothing to close out" from "nobody wrote one", and
    guessing is how a business row gets left in an active state that no screen
    can then move: rebuild refused because a build looks live, delete refused
    for the same reason, and no way out but the database.
    """
    recovered = 0
    with session_factory() as session:
        # Before any task is touched. A `prepared` publication is a run whose
        # rows are committed but whose bytes may still be in staging; leaving it
        # unresolved would let a later reader open a directory that is not there
        # (see research/publication.py).
        failed_runs = recover_publications(session, get_settings().research_artifact_dir)
        if failed_runs:
            logger.warning("artifact_publication.recovered_as_failed", runs=len(failed_runs))

        orphans = session.scalars(select(Task).where(Task.status == TaskStatus.RUNNING)).all()
        for task in orphans:
            bind_contextvars(task_id=str(task.id), task_type=task.task_type)
            _fail_orphaned_task(task)

            recovery = get_recovery(task.task_type)
            if recovery is None:
                logger.warning("task.orphaned_without_recovery")
            else:
                recovery(session, task)
                logger.warning("task.orphaned_on_restart", status=task.status.value)
            recovered += 1

        session.commit()
        clear_contextvars()
    return recovered


def _fail_orphaned_task(task: Task) -> None:
    """The default outcome for every orphan, applied before its recovery runs.

    Recoveries overwrite it when the task is resumable, so this is what a task
    ends up with when nothing knows better — never a row left at RUNNING.
    """
    task.status = TaskStatus.FAILED
    task.error = ORPHAN_ERROR
    task.finished_at = datetime.now(timezone.utc)


def _claim_next_task(session: Session) -> Task | None:
    task = session.scalars(
        select(Task).where(Task.status == TaskStatus.QUEUED).order_by(Task.created_at).limit(1)
    ).first()
    if task is None:
        return None
    task.status = TaskStatus.RUNNING
    task.attempt_count += 1
    task.started_at = datetime.now(timezone.utc)
    # Only the task is claimed here. Whatever business row it drives is the
    # handler's to move, and the sync workflow already does it under a row lock
    # a moment later — doing it again from out here would be a second, unlocked
    # writer to the same row for no gain.
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
        # Not necessarily SUCCEEDED: a handler that owns its own terminal state
        # may have already committed CANCELLED here, and reporting that as a
        # success is how a cancelled run comes to look like a finished one.
        logger.info("task.finished", status=task.status.value)
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
