from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import shutil
import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.market_data import DataSnapshot, TradingCalendar
from app.core.config import get_settings
from app.models.research import (
    ACTIVE_RESEARCH_STATUSES,
    BundleStatus,
    DataBundleBuildAttempt,
    QlibDataBundle,
    ResearchArtifact,
    ResearchExperiment,
    ResearchRun,
    ResearchRunStatus,
)
from app.models.task import Task, TaskStatus
from app.research.artifacts import ResearchArtifactReader
from app.research.bundle_builder import EXPORTER_SCHEMA_VERSION, PYQLIB_VERSION
from app.research.definition import ResearchDefinition
from app.services.stock_pool import DEFAULT_POLICY, policy_fingerprint


class SqlResearchApplication:
    """Application use cases for experiments and runs.

    The first API slice injects this class behind a protocol; persistence is
    filled in by the next slice so the HTTP contract can remain stable.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_config(self) -> dict:
        snapshot = self.session.scalar(
            select(DataSnapshot)
            .where(DataSnapshot.is_backtest_eligible.is_(True))
            .order_by(DataSnapshot.created_at.desc(), DataSnapshot.version.desc())
            .limit(1)
        )
        if snapshot is None:
            return {
                "default_snapshot": None,
                "observation_start": None,
                "observation_end": None,
                "evaluation_end": None,
                "lookback_days": 126,
                "skip_days": 21,
                "factor_coverage": 0.90,
                "label_coverage": 0.90,
                "min_valid_securities": 100,
            }
        calendar = list(
            self.session.scalars(
                select(TradingCalendar.trade_date)
                .where(
                    TradingCalendar.publication_id == snapshot.calendar_publication_id,
                    TradingCalendar.is_open.is_(True),
                    TradingCalendar.trade_date >= snapshot.coverage_start,
                    TradingCalendar.trade_date <= snapshot.coverage_end,
                )
                .order_by(TradingCalendar.trade_date)
            ).all()
        )
        observation_start = calendar[147] if len(calendar) > 147 else None
        observation_end = calendar[-1] if calendar else None
        evaluation_end = calendar[-7] if len(calendar) >= 7 else None
        return {
            "default_snapshot": {"id": str(snapshot.id), "version": snapshot.version},
            "observation_start": observation_start,
            "observation_end": observation_end,
            "evaluation_end": evaluation_end,
            "lookback_days": 126,
            "skip_days": 21,
            "factor_coverage": 0.90,
            "label_coverage": 0.90,
            "min_valid_securities": 100,
        }

    def create_run(self, request) -> dict:
        snapshot = self.session.get(DataSnapshot, request.data_snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Data snapshot not found")
        if not snapshot.is_backtest_eligible:
            raise HTTPException(status_code=409, detail="Data snapshot is not research eligible")
        if not (
            snapshot.coverage_start <= request.observation_start <= request.observation_end <= snapshot.coverage_end
        ):
            raise HTTPException(status_code=400, detail="Observation range is outside snapshot coverage")

        required = request.lookback_days + request.skip_days
        policy = replace(
            DEFAULT_POLICY,
            required_history_days=required,
            required_bar_offsets=(required, request.skip_days),
        )
        definition = ResearchDefinition(
            data_snapshot_id=request.data_snapshot_id,
            observation_start=request.observation_start,
            observation_end=request.observation_end,
            lookback_days=request.lookback_days,
            skip_days=request.skip_days,
            stock_pool_policy_fingerprint=policy_fingerprint(policy),
        )
        experiment = self.session.scalar(
            select(ResearchExperiment).where(
                ResearchExperiment.definition_fingerprint == definition.fingerprint
            )
        )
        if experiment is None:
            experiment = ResearchExperiment(
                definition_fingerprint=definition.fingerprint,
                data_snapshot_id=request.data_snapshot_id,
                definition=definition.canonical_payload,
                observation_start=request.observation_start,
                observation_end=request.observation_end,
                lookback_days=request.lookback_days,
                skip_days=request.skip_days,
            )
            self.session.add(experiment)
            self.session.flush()

        active = self.session.scalar(
            select(ResearchRun)
            .where(
                ResearchRun.experiment_id == experiment.id,
                ResearchRun.status.in_(ACTIVE_RESEARCH_STATUSES),
            )
            .order_by(ResearchRun.created_at.desc())
        )
        if active is not None:
            return serialize_run(active)

        previous = self.session.scalar(
            select(ResearchRun)
            .where(ResearchRun.experiment_id == experiment.id)
            .order_by(ResearchRun.created_at.desc())
            .limit(1)
        )
        task = Task(task_type="momentum_research", status=TaskStatus.QUEUED, payload={})
        self.session.add(task)
        self.session.flush()
        run = ResearchRun(
            experiment_id=experiment.id,
            previous_run_id=previous.id if previous else None,
            task_id=task.id,
            status=ResearchRunStatus.QUEUED,
        )
        self.session.add(run)
        self.session.flush()
        task.payload = {"research_run_id": str(run.id)}
        self.session.commit()
        return serialize_run(run)

    def list_runs(self) -> list[dict]:
        runs = self.session.scalars(
            select(ResearchRun).order_by(ResearchRun.created_at.desc()).limit(50)
        ).all()
        return [serialize_run(run) for run in runs]

    def get_run(self, run_id) -> dict:
        run = self.session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Research run not found")
        return serialize_run(run)

    def cancel_run(self, run_id) -> dict:
        run = self.session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Research run not found")
        if run.status not in ACTIVE_RESEARCH_STATUSES:
            return serialize_run(run)
        run.cancel_requested = True
        if run.status == ResearchRunStatus.QUEUED:
            now = datetime.now(timezone.utc)
            run.status = ResearchRunStatus.CANCELLED
            run.finished_at = now
            task = self.session.get(Task, run.task_id)
            task.status = TaskStatus.CANCELLED
            task.finished_at = now
        self.session.commit()
        return serialize_run(run)

    def get_results(self, run_id) -> dict:
        run = self.session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Research run not found")
        artifact = self.session.scalar(
            select(ResearchArtifact).where(ResearchArtifact.research_run_id == run_id)
        )
        if artifact is None:
            raise HTTPException(status_code=409, detail="Research artifact is not published")
        summary = ResearchArtifactReader(get_settings().research_artifact_dir).summary(
            artifact.relative_path
        )
        reader = ResearchArtifactReader(get_settings().research_artifact_dir)
        daily = reader.table(artifact.relative_path, "daily_metrics")
        weekly = reader.table(artifact.relative_path, "weekly_metrics")
        return {
            "run": serialize_run(run),
            "summary": summary,
            "manifest": artifact.manifest,
            "daily_metrics": json.loads(daily.to_json(orient="records", date_format="iso")),
            "weekly_metrics": json.loads(weekly.to_json(orient="records", date_format="iso")),
        }

    def get_ranked_scores(self, run_id, observation_date=None) -> dict:
        run = self.session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Research run not found")
        artifact = self.session.scalar(
            select(ResearchArtifact).where(ResearchArtifact.research_run_id == run_id)
        )
        if artifact is None:
            raise HTTPException(status_code=409, detail="Research artifact is not published")
        reader = ResearchArtifactReader(get_settings().research_artifact_dir)
        scores = reader.table(artifact.relative_path, "scores")
        exclusions = reader.table(artifact.relative_path, "exclusions")
        if observation_date is not None:
            values = scores["observation_date"].astype(str)
            scores = scores[values == observation_date.isoformat()]
            if "observation_date" in exclusions:
                values = exclusions["observation_date"].astype(str)
                exclusions = exclusions[values == observation_date.isoformat()]
        return {
            "run_id": str(run_id),
            "data_snapshot_id": str(
                self.session.get(ResearchExperiment, run.experiment_id).data_snapshot_id
            ),
            "observation_date": observation_date,
            "scores": json.loads(scores.to_json(orient="records", date_format="iso")),
            "exclusions": json.loads(
                exclusions.to_json(orient="records", date_format="iso")
            ),
        }

    def list_bundles(self) -> list[dict]:
        bundles = self.session.scalars(
            select(QlibDataBundle).order_by(QlibDataBundle.created_at.desc())
        ).all()
        return [self._bundle(item) for item in bundles]

    def prebuild_bundle(self, snapshot_id) -> dict:
        snapshot = self.session.get(DataSnapshot, snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Data snapshot not found")
        if not snapshot.is_backtest_eligible:
            raise HTTPException(status_code=409, detail="Data snapshot is not research eligible")
        bundle = self.session.scalar(
            select(QlibDataBundle).where(
                QlibDataBundle.data_snapshot_id == snapshot_id,
                QlibDataBundle.exporter_schema_version == EXPORTER_SCHEMA_VERSION,
                QlibDataBundle.pyqlib_version == PYQLIB_VERSION,
            )
        )
        if bundle and bundle.status in {
            BundleStatus.QUEUED,
            BundleStatus.BUILDING,
            BundleStatus.VALIDATING,
            BundleStatus.PUBLISHING,
            BundleStatus.READY,
        }:
            return self._bundle(bundle)
        if bundle is None:
            bundle = QlibDataBundle(
                data_snapshot_id=snapshot_id,
                exporter_schema_version=EXPORTER_SCHEMA_VERSION,
                pyqlib_version=PYQLIB_VERSION,
                status=BundleStatus.QUEUED,
            )
            self.session.add(bundle)
            self.session.flush()
        else:
            bundle.status = BundleStatus.QUEUED
        task = Task(task_type="qlib_bundle_build", status=TaskStatus.QUEUED, payload={})
        self.session.add(task)
        self.session.flush()
        task.payload = {"data_snapshot_id": str(snapshot_id), "task_id": str(task.id)}
        self.session.commit()
        return self._bundle(bundle)

    def delete_bundle(self, bundle_id) -> dict:
        bundle = self.session.get(QlibDataBundle, bundle_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="QlibDataBundle not found")
        if bundle.status in {BundleStatus.BUILDING, BundleStatus.VALIDATING, BundleStatus.PUBLISHING}:
            raise HTTPException(status_code=409, detail="Active QlibDataBundle cannot be deleted")
        active = self.session.scalar(
            select(ResearchRun).where(
                ResearchRun.bundle_id == bundle_id,
                ResearchRun.status.in_(ACTIVE_RESEARCH_STATUSES),
            )
        )
        if active is not None:
            raise HTTPException(status_code=409, detail="QlibDataBundle is used by an active run")
        if bundle.relative_path:
            root = get_settings().qlib_data_dir.resolve()
            target = (root / bundle.relative_path).resolve()
            if root not in target.parents:
                raise HTTPException(status_code=500, detail="Bundle path escapes configured root")
            if target.exists():
                shutil.rmtree(target)
        bundle.status = BundleStatus.DELETED
        bundle.relative_path = None
        self.session.commit()
        return self._bundle(bundle)

    def _bundle(self, bundle: QlibDataBundle) -> dict:
        attempts = self.session.scalars(
            select(DataBundleBuildAttempt)
            .where(DataBundleBuildAttempt.bundle_id == bundle.id)
            .order_by(DataBundleBuildAttempt.created_at.desc())
        ).all()
        return {
            "id": str(bundle.id),
            "data_snapshot_id": str(bundle.data_snapshot_id),
            "status": bundle.status.value,
            "pyqlib_version": bundle.pyqlib_version,
            "exporter_schema_version": bundle.exporter_schema_version,
            "size_bytes": bundle.size_bytes,
            "last_used_at": bundle.last_used_at,
            "error_summary": bundle.error_summary,
            "deletable": bundle.status in {BundleStatus.READY, BundleStatus.FAILED, BundleStatus.DELETED},
            "attempts": [
                {"id": str(item.id), "status": item.status.value, "error_summary": item.error_summary}
                for item in attempts
            ],
        }


def serialize_run(run: ResearchRun) -> dict:
    return {
        "id": str(run.id),
        "experiment_id": str(run.experiment_id),
        "status": run.status.value,
        "current_date": run.current_date,
        "processed_dates": run.processed_dates,
        "total_dates": run.total_dates,
        "warnings": run.warnings,
        "summary": run.summary,
        "error_code": run.error_code,
        "error_summary": run.error_summary,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }
