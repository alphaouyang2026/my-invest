import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.core.logging import get_logger
from app.models.task import Task, TaskStatus
from app.worker.registry import get_handler

logger = get_logger(__name__)

POLL_INTERVAL_SECONDS = 3


def recover_orphaned_tasks(session_factory: sessionmaker[Session]) -> int:
    """Tasks left RUNNING by a worker that crashed/was killed are marked
    FAILED on the next worker startup — never silently resumed, since safe
    resumption would need checkpointing that doesn't exist yet."""
    with session_factory() as session:
        orphaned = session.scalars(select(Task).where(Task.status == TaskStatus.RUNNING)).all()
        for task in orphaned:
            bind_contextvars(task_id=str(task.id))
            logger.warning("task.orphaned_on_restart", task_type=task.task_type)
            task.status = TaskStatus.FAILED
            task.error = "Orphaned by worker restart while RUNNING"
            task.finished_at = datetime.now(timezone.utc)
        session.commit()
        clear_contextvars()
        return len(orphaned)


def _claim_next_task(session: Session) -> Task | None:
    task = session.scalars(
        select(Task).where(Task.status == TaskStatus.QUEUED).order_by(Task.created_at).limit(1)
    ).first()
    if task is None:
        return None
    task.status = TaskStatus.RUNNING
    task.started_at = datetime.now(timezone.utc)
    session.commit()
    return task


def _run_task(session: Session, task: Task) -> None:
    bind_contextvars(task_id=str(task.id), task_type=task.task_type)
    handler = get_handler(task.task_type)
    try:
        if handler is None:
            raise ValueError(f"No handler registered for task_type={task.task_type!r}")
        result = handler(task.payload) or {}
        task.progress = result
        task.status = TaskStatus.SUCCEEDED
        logger.info("task.succeeded")
    except Exception as exc:  # noqa: BLE001 — task failures must never crash the worker loop
        task.status = TaskStatus.FAILED
        task.error = str(exc)
        logger.exception("task.failed")
    finally:
        task.finished_at = datetime.now(timezone.utc)
        session.commit()
        clear_contextvars()


def run_forever(session_factory: sessionmaker[Session]) -> None:
    """Single-process, serial task execution — no Redis/Kafka/distributed
    queue (spec §5.3). Polls on an interval rather than Postgres LISTEN/NOTIFY:
    simpler, and more robust across worker restarts."""
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
