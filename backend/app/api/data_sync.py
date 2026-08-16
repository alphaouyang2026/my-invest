"""Data-sync HTTP surface.

`sync now` takes no date range and no batch size: those are `SyncPolicy`, not
user interface (docs/design/jquants-continuous-batch-sync.md §15.1). The route
handlers only create or inspect work — every J-Quants request happens in the
separate worker process.
"""

import uuid

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import resolve_jquants_api_key
from app.db.session import get_db, get_sessionmaker
from app.models.market_data import EndpointPublication, SyncRun
from app.models.task import Task
from app.services.jquants_sync_workflow import (
    JQuantsSyncWorkflow,
    SyncConflict,
    SyncInvariantError,
    SyncNow,
    is_resumable,
)

router = APIRouter(prefix="/data-sync", tags=["data-sync"])


class JQuantsSyncRequest(BaseModel):
    """Intentionally empty: an idempotent `sync now` with no knobs."""


def get_sync_workflow() -> JQuantsSyncWorkflow:
    return JQuantsSyncWorkflow(get_sessionmaker())


def _summary(db: Session, run: SyncRun, task: Task | None) -> dict:
    """The list/status shape. Field names deliberately match the detail view's,
    so the UI can render a run from either endpoint."""
    return {
        "id": str(run.id),
        "task_id": str(run.task_id),
        "source": run.source,
        "mode": run.mode.value if run.mode else None,
        "status": run.status.value,
        "phase": run.phase.value,
        "target_dates": run.target_dates,
        "processed_dates": run.processed_dates,
        "total_batches": run.total_batches,
        "completed_batches": run.completed_batches,
        "current_batch": run.current_batch,
        "rows_received": run.rows_received,
        "rows_new": run.rows_new,
        "rows_changed": run.rows_changed,
        "rows_unchanged": run.rows_unchanged,
        "planned_start": run.planned_start,
        "planned_end": run.planned_end,
        "actual_min": run.actual_min,
        "actual_max": run.actual_max,
        "coverage_current": run.actual_max,
        "resumable": is_resumable(db, run, task),
        "error_code": run.error_code,
        "error_summary": run.error_summary,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _summaries(db: Session, runs) -> list[dict]:
    """One query for the tasks, rather than a lookup per run."""
    tasks = {
        task.id: task
        for task in db.scalars(select(Task).where(Task.id.in_([run.task_id for run in runs]))).all()
    } if runs else {}
    return [_summary(db, run, tasks.get(run.task_id)) for run in runs]


@router.get("/status")
def data_source_status(db: Session = Depends(get_db)) -> dict:
    try:
        key = resolve_jquants_api_key()
        configuration = "configured" if key else "not_configured"
    except ValueError:
        configuration = "invalid"
    latest = db.scalars(select(SyncRun).order_by(SyncRun.created_at.desc()).limit(1)).first()
    if latest and latest.error_summary and ("HTTP 401" in latest.error_summary or "HTTP 403" in latest.error_summary):
        configuration = "invalid"
    return {
        "source": "jquants",
        "configuration": configuration,
        "plan_notice": "Free 数据约延迟 12 周，可见历史约 2 年；以实测覆盖范围为准。",
        "latest_run": _summaries(db, [latest])[0] if latest else None,
    }


@router.post("/jquants", status_code=status.HTTP_202_ACCEPTED)
def create_jquants_sync(
    request: JQuantsSyncRequest = Body(default=JQuantsSyncRequest()),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    workflow: JQuantsSyncWorkflow = Depends(get_sync_workflow),
) -> dict:
    try:
        if not resolve_jquants_api_key():
            raise HTTPException(status_code=503, detail="J-Quants API key is not configured")
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return workflow.start(SyncNow(), idempotency_key=idempotency_key).to_dict()


@router.get("/runs")
def list_sync_runs(db: Session = Depends(get_db)) -> list[dict]:
    runs = db.scalars(select(SyncRun).order_by(SyncRun.created_at.desc()).limit(50)).all()
    return _summaries(db, list(runs))


@router.get("/runs/{run_id}")
def get_sync_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    workflow: JQuantsSyncWorkflow = Depends(get_sync_workflow),
) -> dict:
    try:
        view = workflow.inspect(run_id).to_dict()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Sync run not found") from exc
    publications = db.scalars(
        select(EndpointPublication)
        .where(EndpointPublication.sync_run_id == run_id)
        .order_by(EndpointPublication.created_at)
    ).all()
    view["publications"] = [
        {
            "endpoint": item.endpoint,
            "scope_ordinal": item.scope_ordinal,
            "attempt": item.attempt,
            "status": item.status.value,
            "publish_sequence": item.publish_sequence,
            "stats": item.stats,
            "error_code": item.error_code,
            "error_summary": item.error_summary,
            "published_at": item.published_at,
        }
        for item in publications
    ]
    return view


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_sync_run(
    run_id: uuid.UUID, workflow: JQuantsSyncWorkflow = Depends(get_sync_workflow)
) -> dict:
    try:
        return workflow.request_cancel(run_id).to_dict()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Sync run not found") from exc
    except SyncConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/force-retry", status_code=status.HTTP_202_ACCEPTED)
def force_retry_sync_run(
    run_id: uuid.UUID, workflow: JQuantsSyncWorkflow = Depends(get_sync_workflow)
) -> dict:
    """Separate from resume on purpose: this overturns a verdict the system
    already reached, so it should not share an entry point with routine
    continuation."""
    try:
        return workflow.force_retry(run_id).to_dict()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Sync run not found") from exc
    except SyncConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SyncInvariantError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
def resume_sync_run(
    run_id: uuid.UUID, workflow: JQuantsSyncWorkflow = Depends(get_sync_workflow)
) -> dict:
    try:
        return workflow.resume(run_id).to_dict()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Sync run not found") from exc
    except SyncConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SyncInvariantError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
