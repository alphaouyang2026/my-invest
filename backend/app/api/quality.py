"""Re-validation HTTP surface.

One command and one status read. The command only queues work: the pass runs
several aggregate queries over every bar the head snapshot resolves to — two
million rows in production — which would time out inside a request and hold a
FastAPI worker thread for the duration.
"""

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.db.session import get_sessionmaker
from app.services.quality_revalidation import (
    QualityRevalidationWorkflow,
    RevalidationConflict,
    RevalidationUnavailable,
)

router = APIRouter(prefix="/quality", tags=["quality"])


def get_revalidation_workflow() -> QualityRevalidationWorkflow:
    return QualityRevalidationWorkflow(get_sessionmaker())


@router.post("/revalidations", status_code=status.HTTP_202_ACCEPTED)
def create_revalidation(
    workflow: QualityRevalidationWorkflow = Depends(get_revalidation_workflow),
):
    """Re-judge the head snapshot with today's rules.

    A refusal carries the work already holding the source, not just a message:
    "something else is running" without saying what leaves the caller with
    nowhere to look.
    """
    try:
        return workflow.start().to_dict()
    except RevalidationConflict as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "active_task": exc.active},
        )
    except RevalidationUnavailable as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)}
        )


@router.get("/revalidations/latest")
def latest_revalidation(
    workflow: QualityRevalidationWorkflow = Depends(get_revalidation_workflow),
) -> dict | None:
    """The most recent re-validation, or null before the first one."""
    latest = workflow.latest()
    return latest.to_dict() if latest else None
