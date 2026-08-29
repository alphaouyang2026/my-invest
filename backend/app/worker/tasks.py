"""Task handlers and their crash recoveries.

Deliberately thin: the worker knows nothing about date plans, batches,
publications or snapshot rules — all of that lives behind
`JQuantsSyncWorkflow` (docs/design/jquants-continuous-batch-sync.md §15.2).

Each handler is registered with the recovery for its task type. They are
written together on purpose: a handler that leaves a business row in an active
state owns what happens to that row when the process dies mid-flight, and the
runner has no way to work it out on the row's behalf.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_jquants_api_key
from app.core.logging import get_logger
from app.db.session import get_sessionmaker
from app.integrations.jquants import JQuantsAdapter
from app.models.market_data import (
    TERMINAL_RUN_STATUSES,
    DataSnapshot,
    EndpointPublication,
    PublicationStatus,
    QualityEvaluation,
    QualityEvaluationStatus,
    SyncRun,
    SyncRunStatus,
)
from app.models.research import (
    ACTIVE_BUILD_ATTEMPT_STATUSES,
    ACTIVE_BUNDLE_STATUSES,
    ACTIVE_RESEARCH_STATUSES,
    BuildAttemptStatus,
    BundleStatus,
    DataBundleBuildAttempt,
    QlibDataBundle,
    ResearchRun,
    ResearchRunStatus,
)
from app.models.task import Task, TaskStatus
from app.research.bundle_builder import QlibDataBundleBuilder
from app.research.model_workflow import ModelResearchWorkflow
from app.research.workflow import ResearchWorkflow
from app.services.jquants_sync_workflow import JQuantsSyncWorkflow, SyncPolicy
from app.services.quality_revalidation import TASK_TYPE as REVALIDATION_TASK_TYPE
from app.services.quality_revalidation import QualityRevalidationWorkflow
from app.worker.registry import register

logger = get_logger(__name__)

ORPHANED_WHILE_RUNNING = "Orphaned by worker restart while running"

_RUN_TERMINAL_TO_TASK = {
    SyncRunStatus.SUCCEEDED: TaskStatus.SUCCEEDED,
    SyncRunStatus.NO_CHANGE: TaskStatus.SUCCEEDED,
    SyncRunStatus.FAILED: TaskStatus.FAILED,
    SyncRunStatus.PARTIAL_FAILED: TaskStatus.FAILED,
    SyncRunStatus.CANCELLED: TaskStatus.CANCELLED,
}


# --------------------------------------------------------------- J-Quants sync


def recover_jquants_sync(session: Session, task: Task) -> None:
    """Re-queue a sync against its persisted checkpoint instead of failing it.

    This is the one task type with progress worth resuming: the batch and
    publication records are exactly what survives a crash, so the run picks up
    where it stopped rather than re-fetching everything.
    """
    run = session.scalar(select(SyncRun).where(SyncRun.task_id == task.id).with_for_update())
    if run is None:
        return

    now = datetime.now(timezone.utc)
    if run.status in TERMINAL_RUN_STATUSES:
        # Legacy inconsistency: a terminal run must never be re-queued. Repair
        # the task to match what the run already decided.
        task.status = _RUN_TERMINAL_TO_TASK[run.status]
        task.error = None
        task.finished_at = run.finished_at or now
        logger.warning("task.repaired_against_terminal_run", run_status=run.status.value)
        return

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
        task.error = None
        task.finished_at = now
        logger.info("sync_run.cancel_completed_on_restart")
        return

    if task.attempt_count >= SyncPolicy().max_task_attempts:
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
        run.error_summary = f"Exceeded {SyncPolicy().max_task_attempts} worker attempts"
        run.finished_at = now
        task.error = run.error_summary
        logger.warning("sync_run.attempts_exhausted", attempts=task.attempt_count)
        return

    run.status = SyncRunStatus.QUEUED
    task.status = TaskStatus.QUEUED
    task.error = None
    task.started_at = None
    task.finished_at = None
    logger.info("sync_run.requeued_from_checkpoint", attempt=task.attempt_count)


@register("jquants_sync", recover=recover_jquants_sync)
def run_jquants_sync(payload: dict) -> dict:
    run_id = uuid.UUID(payload["sync_run_id"])
    key = resolve_jquants_api_key()
    if not key:
        raise ValueError("J-Quants API key is not configured")

    workflow = JQuantsSyncWorkflow(get_sessionmaker(), JQuantsAdapter(key))
    return workflow.execute(run_id).to_dict()


# --------------------------------------------------------- quality re-validation


def recover_quality_revalidation(session: Session, task: Task) -> None:
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
    evaluation.error_summary = ORPHANED_WHILE_RUNNING
    logger.warning("quality_evaluation.orphaned_on_restart", evaluation_id=str(evaluation.id))


@register(REVALIDATION_TASK_TYPE, recover=recover_quality_revalidation)
def run_quality_revalidation(payload: dict) -> dict:
    """Re-judge the head snapshot. No API key: it reads only what is stored."""
    evaluation_id = uuid.UUID(payload["evaluation_id"])
    return QualityRevalidationWorkflow(get_sessionmaker()).execute(evaluation_id)


# ------------------------------------------------------------ momentum research


def _fail_orphaned_bundle_build(session: Session, task: Task) -> None:
    """Close out a Qlib bundle build whose worker died mid-export.

    Leaving the bundle in an active state is a dead end, not just an untidy
    row: `ensure_bundle` returns an active bundle instead of queueing a
    rebuild, and `delete_bundle` refuses one, so the bundle screen offers no
    way out. The exporter only ever publishes by atomic rename, so a crash
    leaves nothing usable behind — FAILED is the honest state, and the next
    request rebuilds from scratch.

    A research run builds its bundle under its own task id, so this runs for
    both task types that can leave an attempt open.
    """
    attempts = session.scalars(
        select(DataBundleBuildAttempt)
        .where(
            DataBundleBuildAttempt.task_id == task.id,
            DataBundleBuildAttempt.status.in_(ACTIVE_BUILD_ATTEMPT_STATUSES),
        )
        .with_for_update()
    ).all()

    now = datetime.now(timezone.utc)
    for attempt in attempts:
        attempt.status = BuildAttemptStatus.FAILED
        attempt.error_code = "worker_restart"
        attempt.error_summary = ORPHANED_WHILE_RUNNING
        attempt.finished_at = now

        bundle = session.get(QlibDataBundle, attempt.bundle_id, with_for_update=True)
        if bundle is not None and bundle.status in ACTIVE_BUNDLE_STATUSES:
            bundle.status = BundleStatus.FAILED
            bundle.error_summary = ORPHANED_WHILE_RUNNING
        logger.warning(
            "bundle_build.orphaned_on_restart",
            bundle_id=str(attempt.bundle_id),
            build_attempt_id=str(attempt.id),
        )


def recover_momentum_research(session: Session, task: Task) -> None:
    """Close out a research run whose worker died mid-pass.

    Nothing partial is worth keeping: the artifact is published by atomic
    rename at the very end, so a run that did not reach PUBLISHING left no
    output. Re-running is the recovery, and it is the caller's decision — the
    run is deterministic from its snapshot, so a silent re-queue would repeat
    an expensive pass nobody asked for a second time.
    """
    _fail_orphaned_bundle_build(session, task)

    run = session.scalar(select(ResearchRun).where(ResearchRun.task_id == task.id).with_for_update())
    if run is None or run.status not in ACTIVE_RESEARCH_STATUSES:
        return
    if run.cancel_requested:
        # The cancel was already granted; the crash is not what ended this run.
        run.status = ResearchRunStatus.CANCELLED
        task.status = TaskStatus.CANCELLED
        task.error = None
    else:
        run.status = ResearchRunStatus.FAILED
        run.error_code = "worker_restart"
        run.error_summary = ORPHANED_WHILE_RUNNING
    run.finished_at = datetime.now(timezone.utc)
    logger.warning(
        "research_run.orphaned_on_restart",
        research_run_id=str(run.id),
        status=run.status.value,
    )


@register("momentum_research", recover=recover_momentum_research)
def run_momentum_research(payload: dict) -> dict:
    run_id = uuid.UUID(payload["research_run_id"])
    return ResearchWorkflow(get_sessionmaker()).execute(run_id)


# The same recovery: a model run reaching PUBLISHING leaves a `prepared`
# publication that `recover_publications` resolves at worker start, and one that
# did not left nothing at all. Re-running stays the caller's decision — training
# is expensive and deterministic, so a silent re-queue would repeat a pass
# nobody asked for twice.
@register("model_research", recover=recover_momentum_research)
def run_model_research(payload: dict) -> dict:
    run_id = uuid.UUID(payload["research_run_id"])
    return ModelResearchWorkflow(get_sessionmaker()).execute(run_id)


# ----------------------------------------------------------- Qlib bundle build


@register("qlib_bundle_build", recover=_fail_orphaned_bundle_build)
def run_qlib_bundle_build(payload: dict) -> dict:
    snapshot_id = uuid.UUID(payload["data_snapshot_id"])
    task_id = uuid.UUID(payload["task_id"])
    with get_sessionmaker()() as session:
        snapshot = session.get(DataSnapshot, snapshot_id)
        if snapshot is None:
            raise ValueError("Data snapshot not found")
        bundle = QlibDataBundleBuilder(session, get_settings()).ensure(snapshot, task_id=task_id)
        return {"bundle_id": str(bundle.id), "status": bundle.status.value}
