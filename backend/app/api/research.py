from datetime import date
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db


router = APIRouter(prefix="/research", tags=["research"])
bundle_router = APIRouter(prefix="/qlib-data-bundles", tags=["qlib-data-bundles"])


class CreateResearchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_snapshot_id: UUID
    observation_start: date
    observation_end: date
    lookback_days: int = Field(default=126, ge=1, le=504)
    skip_days: int = Field(default=21, ge=1, le=126)


class CreateModelRunRequest(BaseModel):
    """Everything a model run needs, and nothing it must not be given.

    `seed` is a top-level field and `model_params` refuses every seed key. Two
    entry points would need a precedence rule, and that rule would only ever be
    read by someone who had already sent both and could no longer say which one
    ran — while reproducibility is what this ticket is accepted against.

    Segment boundaries are explicit dates rather than ratios. The page derives
    them from `GET /research/model-runs/config`, but what is submitted, stored
    and fingerprinted is the dates themselves.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    data_snapshot_id: UUID
    feature_set: str = "alpha158_jp_v1"
    train_start: date
    train_end: date
    valid_start: date
    valid_end: date
    test_start: date
    test_end: date
    seed: int = Field(default=20260829, ge=0, lt=2**31)
    #: Only the keys in the whitelist, only inside their ranges; anything else
    #: is a 400 rather than a silently ignored or clamped value.
    model_params: dict[str, float] | None = None


class ModelResearchApplication(Protocol):
    def get_config(self) -> dict: ...
    def list_feature_sets(self) -> list[dict]: ...
    def create_run(self, request: CreateModelRunRequest) -> dict: ...
    def get_results(self, run_id: UUID) -> dict: ...
    def get_ranked_scores(self, run_id: UUID, observation_date: date | None) -> dict: ...


def get_model_application(db: Session = Depends(get_db)) -> ModelResearchApplication:
    from app.services.model_research import SqlModelResearchApplication

    return SqlModelResearchApplication(db)


class ResearchApplication(Protocol):
    def get_config(self) -> dict: ...
    def create_run(self, request: CreateResearchRunRequest) -> dict: ...
    def list_runs(self) -> list[dict]: ...
    def get_run(self, run_id: UUID) -> dict: ...
    def cancel_run(self, run_id: UUID) -> dict: ...
    def get_results(self, run_id: UUID) -> dict: ...
    def get_ranked_scores(self, run_id: UUID, observation_date: date | None) -> dict: ...
    def list_bundles(self) -> list[dict]: ...
    def prebuild_bundle(self, snapshot_id: UUID) -> dict: ...
    def delete_bundle(self, bundle_id: UUID) -> dict: ...


def get_research_application(db: Session = Depends(get_db)) -> ResearchApplication:
    from app.services.research import SqlResearchApplication

    return SqlResearchApplication(db)


@router.get("/config")
def get_research_config(
    application: ResearchApplication = Depends(get_research_application),
) -> dict:
    return application.get_config()


@router.post("/runs", status_code=202)
def create_research_run(
    request: CreateResearchRunRequest,
    application: ResearchApplication = Depends(get_research_application),
) -> dict:
    return application.create_run(request)


@router.get("/runs")
def list_research_runs(
    application: ResearchApplication = Depends(get_research_application),
) -> list[dict]:
    return application.list_runs()


@router.get("/runs/{run_id}")
def get_research_run(
    run_id: UUID,
    application: ResearchApplication = Depends(get_research_application),
) -> dict:
    return application.get_run(run_id)


@router.post("/runs/{run_id}/cancel")
def cancel_research_run(
    run_id: UUID,
    application: ResearchApplication = Depends(get_research_application),
) -> dict:
    return application.cancel_run(run_id)


@router.get("/runs/{run_id}/results")
def get_research_results(
    run_id: UUID,
    application: ResearchApplication = Depends(get_research_application),
) -> dict:
    return application.get_results(run_id)


@router.get("/runs/{run_id}/ranked-scores")
def get_ranked_scores(
    run_id: UUID,
    observation_date: date | None = None,
    application: ResearchApplication = Depends(get_research_application),
) -> dict:
    """One URL for both kinds of run, returning one shape.

    Which kind it is, is resolved inside the application rather than here.
    Dispatching in the route would mean depending on a session directly, past
    the seam every other endpoint goes through — and `RankedScores` exists
    precisely so a caller never has to know which kind produced a score.
    """
    return application.get_ranked_scores(run_id, observation_date)


@router.get("/feature-sets")
def list_feature_sets(
    application: ModelResearchApplication = Depends(get_model_application),
) -> list[dict]:
    return application.list_feature_sets()


@router.get("/model-runs/config")
def get_model_run_config(
    application: ModelResearchApplication = Depends(get_model_application),
) -> dict:
    return application.get_config()


@router.post("/model-runs", status_code=202)
def create_model_run(
    request: CreateModelRunRequest,
    application: ModelResearchApplication = Depends(get_model_application),
) -> dict:
    return application.create_run(request)


@router.get("/model-runs/{run_id}/results")
def get_model_run_results(
    run_id: UUID,
    application: ModelResearchApplication = Depends(get_model_application),
) -> dict:
    return application.get_results(run_id)


class PrebuildBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_snapshot_id: UUID


@bundle_router.get("")
def list_data_bundles(
    application: ResearchApplication = Depends(get_research_application),
) -> list[dict]:
    return application.list_bundles()


@bundle_router.post("", status_code=202)
def prebuild_data_bundle(
    request: PrebuildBundleRequest,
    application: ResearchApplication = Depends(get_research_application),
) -> dict:
    return application.prebuild_bundle(request.data_snapshot_id)


@bundle_router.delete("/{bundle_id}")
def delete_data_bundle(
    bundle_id: UUID,
    application: ResearchApplication = Depends(get_research_application),
) -> dict:
    return application.delete_bundle(bundle_id)
