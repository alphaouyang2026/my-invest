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
    return application.get_ranked_scores(run_id, observation_date)


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
