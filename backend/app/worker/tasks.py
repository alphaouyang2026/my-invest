"""Task handlers. Deliberately thin: the worker knows nothing about date
plans, batches, publications or snapshot rules — all of that lives behind
`JQuantsSyncWorkflow` (docs/design/jquants-continuous-batch-sync.md §15.2)."""

import uuid

from app.core.config import resolve_jquants_api_key
from app.db.session import get_sessionmaker
from app.integrations.jquants import JQuantsAdapter
from app.services.jquants_sync_workflow import JQuantsSyncWorkflow
from app.services.quality_revalidation import TASK_TYPE as REVALIDATION_TASK_TYPE
from app.services.quality_revalidation import QualityRevalidationWorkflow
from app.worker.registry import register


@register("jquants_sync")
def run_jquants_sync(payload: dict) -> dict:
    run_id = uuid.UUID(payload["sync_run_id"])
    key = resolve_jquants_api_key()
    if not key:
        raise ValueError("J-Quants API key is not configured")

    workflow = JQuantsSyncWorkflow(get_sessionmaker(), JQuantsAdapter(key))
    return workflow.execute(run_id).to_dict()


@register(REVALIDATION_TASK_TYPE)
def run_quality_revalidation(payload: dict) -> dict:
    """Re-judge the head snapshot. No API key: it reads only what is stored."""
    evaluation_id = uuid.UUID(payload["evaluation_id"])
    return QualityRevalidationWorkflow(get_sessionmaker()).execute(evaluation_id)
