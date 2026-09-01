"""Application use cases for model runs.

Creation is a separate endpoint from ticket 06's because the parameters really
are different — a feature set, three segment boundaries, a whitelist of
overrides. Reading ranked scores is *not* separate, and that asymmetry is the
whole point: `RankedScores` exists so ticket 08 need not know whether a score
came from a factor or a model, and one divergent field would make it write the
consumer twice.

Nothing here initialises Qlib. The API process resolves an experiment, queues a
task and reads published artifacts; the worker does the rest.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.market_data import DataSnapshot, Instrument, TradingCalendar
from app.models.research import (
    ACTIVE_RESEARCH_STATUSES,
    PredictionRun,
    ResearchExperiment,
    ResearchExperimentKind,
    ResearchRun,
    ResearchRunStatus,
    TrainedModel,
)
from app.models.task import Task, TaskStatus
from app.research.artifacts import ResearchArtifactReader
from app.research.feature_sets import UnknownFeatureSetError, get_feature_set, list_feature_sets
from app.research.model_definition import (
    DEFAULT_FEATURE_SET,
    DEFAULT_SEED,
    LOCKED_PARAMS,
    MOMENTUM_SKIP_DAYS,
    OVERRIDABLE_PARAMS,
    ModelResearchDefinition,
    ParameterError,
    model_required_history_days,
    resolve_model_params,
)
from app.research.publication import readable_artifact
from app.research.splits import SplitError, build_split_plan, propose_split
from app.services.research import serialize_run
from app.services.stock_pool import DEFAULT_POLICY, policy_fingerprint

MODEL_TASK_TYPE = "model_research"


class SqlModelResearchApplication:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- reference data ----------------------------------------------------

    def list_feature_sets(self) -> list[dict]:
        """The registry, read-only.

        `selectable`, `is_default` and `maturity` drive what the page offers.
        They are absent from the experiment fingerprint on purpose — they steer
        wording and defaults, not what any column computes.
        """
        return [
            {
                "name": item.name,
                "version": item.version,
                "description": item.description,
                "column_count": len(item.features),
                "required_fields": list(item.required_fields),
                "max_window": item.max_window,
                "selectable": item.selectable,
                "is_default": item.is_default,
                "maturity": item.maturity,
                "definition_checksum": item.definition_checksum,
                "columns": [feature.as_payload() for feature in item.features],
            }
            for item in list_feature_sets()
        ]

    def get_config(self) -> dict:
        """Defaults for the creation form, derived rather than guessed."""
        snapshot = self.session.scalar(
            select(DataSnapshot)
            .where(DataSnapshot.is_backtest_eligible.is_(True))
            .order_by(DataSnapshot.created_at.desc(), DataSnapshot.version.desc())
            .limit(1)
        )
        payload: dict = {
            "default_snapshot": None,
            "feature_sets": self.list_feature_sets(),
            "default_feature_set": DEFAULT_FEATURE_SET,
            "default_seed": DEFAULT_SEED,
            "locked_params": LOCKED_PARAMS,
            "overridable_params": {
                name: {"type": spec[0].__name__, "min": spec[1], "max": spec[2]}
                for name, spec in OVERRIDABLE_PARAMS.items()
            },
            "splits": None,
        }
        if snapshot is None:
            return payload
        payload["default_snapshot"] = {"id": str(snapshot.id), "version": snapshot.version}
        weekly = self._weekly_observations(snapshot)
        try:
            payload["splits"] = {
                key: value.isoformat() for key, value in propose_split(weekly).items()
            }
        except SplitError as exc:
            # Not an error response: the page has to be able to say *why* it
            # cannot offer a default, and "this snapshot is too short" is a
            # fact about the data rather than a bad request.
            payload["splits_unavailable_reason"] = str(exc)
        return payload

    # -- runs --------------------------------------------------------------

    def create_run(self, request) -> dict:
        snapshot = self.session.get(DataSnapshot, request.data_snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Data snapshot not found")
        if not snapshot.is_backtest_eligible:
            raise HTTPException(status_code=409, detail="Data snapshot is not research eligible")

        try:
            feature_set = get_feature_set(request.feature_set)
        except UnknownFeatureSetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not feature_set.selectable:
            raise HTTPException(
                status_code=400,
                detail=f"Feature set {feature_set.name!r} is a control and cannot be trained on",
            )

        try:
            model_params = resolve_model_params(request.model_params, seed=request.seed)
        except ParameterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        weekly = self._weekly_observations(snapshot)
        calendar = self._calendar(snapshot)
        try:
            plan = build_split_plan(
                weekly_observations=weekly,
                calendar=calendar,
                train_start=request.train_start,
                train_end=request.train_end,
                valid_start=request.valid_start,
                valid_end=request.valid_end,
                test_start=request.test_start,
                test_end=request.test_end,
            )
        except SplitError as exc:
            # 400 rather than a repaired split: silently moving a boundary
            # would leave the caller believing they trained on a range they did
            # not, while the fingerprint records the other one.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        required_history_days = model_required_history_days(feature_set)
        policy = replace(
            DEFAULT_POLICY,
            required_history_days=required_history_days,
            required_bar_offsets=(required_history_days, MOMENTUM_SKIP_DAYS),
        )
        definition = ModelResearchDefinition(
            data_snapshot_id=snapshot.id,
            feature_set=feature_set,
            split=plan,
            stock_pool_policy_fingerprint=policy_fingerprint(policy),
            model_params=model_params,
            seed=request.seed,
        )
        experiment = self.session.scalar(
            select(ResearchExperiment).where(
                ResearchExperiment.definition_fingerprint == definition.fingerprint
            )
        )
        if experiment is None:
            experiment = ResearchExperiment(
                definition_fingerprint=definition.fingerprint,
                kind=ResearchExperimentKind.MODEL,
                data_snapshot_id=snapshot.id,
                definition=definition.canonical_payload,
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
        task = Task(task_type=MODEL_TASK_TYPE, status=TaskStatus.QUEUED, payload={})
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

    def get_results(self, run_id: UUID) -> dict:
        run, artifact = self._published(run_id)
        reader = ResearchArtifactReader(get_settings().research_artifact_dir)
        trained = self.session.scalar(
            select(TrainedModel).where(TrainedModel.research_run_id == run_id)
        )
        prediction = self.session.scalar(
            select(PredictionRun).where(PredictionRun.research_run_id == run_id)
        )
        return {
            "run": serialize_run(run),
            "summary": reader.summary(artifact.relative_path),
            "manifest": artifact.manifest,
            "trained_model": self._serialize_model(trained),
            "prediction_run": self._serialize_prediction(prediction),
            **{
                table: _records(reader.table(artifact.relative_path, table))
                for table in (
                    "metrics",
                    "feature_importance",
                    "feature_missing_rate",
                    "feature_group_anomalies",
                    "training_curve",
                    "qlib_signal_analysis",
                )
            },
        }

    def get_ranked_scores(self, run_id: UUID, observation_date: date | None) -> dict:
        """Field-for-field the shape ticket 06 returns.

        The two kinds of run differ in how a score was produced and in nothing a
        consumer can see. Ticket 08 reads `rank_percentile` and does not ask
        which endpoint created it.
        """
        run, artifact = self._published(run_id)
        reader = ResearchArtifactReader(get_settings().research_artifact_dir)
        scores = reader.table(artifact.relative_path, "predictions")
        # The security code is resolved here rather than stored in the artifact.
        # It lives on `instruments.source_code` — one row per stable identity —
        # so a published artifact carries the identity and the display label is
        # looked up, which also makes every already-published run readable
        # without a re-run.
        scores["source_code"] = scores["instrument_id"].map(_instrument_codes(self.session))
        if observation_date is not None:
            values = scores["observation_date"].astype(str)
            scores = scores[values == observation_date.isoformat()]
        return {
            "run_id": str(run_id),
            "data_snapshot_id": str(
                self.session.get(ResearchExperiment, run.experiment_id).data_snapshot_id
            ),
            "observation_date": observation_date,
            "scores": _records(scores),
            # A model scores every member of the frozen universe it was given,
            # so there is no second population to exclude. The key is present
            # because ticket 06 returns it and the shapes have to match.
            "exclusions": [],
        }

    # -- helpers -----------------------------------------------------------

    def _published(self, run_id: UUID):
        run = self.session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Research run not found")
        artifact = readable_artifact(self.session, run_id)
        if artifact is None:
            raise HTTPException(status_code=409, detail="Research artifact is not published")
        return run, artifact

    def _calendar(self, snapshot: DataSnapshot) -> list[date]:
        return list(
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

    def _weekly_observations(self, snapshot: DataSnapshot) -> list[date]:
        """Weekly cross-sections a run could actually use.

        The first 147 sessions are dropped because every pool member has to have
        that much history behind it; offering a boundary inside the warm-up
        would produce a form whose defaults cannot run.
        """
        calendar = self._calendar(snapshot)[DEFAULT_POLICY.required_history_days :]
        by_week: dict[tuple[int, int], date] = {}
        for day in calendar:
            by_week[day.isocalendar()[:2]] = day
        return sorted(by_week.values())

    @staticmethod
    def _serialize_model(model: TrainedModel | None) -> dict | None:
        if model is None:
            return None
        return {
            "id": str(model.id),
            "feature_set": {"name": model.feature_set_name, "version": model.feature_set_version},
            "label_definition": model.label_definition,
            "segments": {
                "train": [model.train_start, model.train_end],
                "valid": [model.valid_start, model.valid_end],
                "test": [model.test_start, model.test_end],
            },
            "fit_window": [model.fit_start, model.fit_end],
            "seed": model.seed,
            "best_iteration": model.best_iteration,
            "model_params": model.model_params,
            "runtime_identity": model.runtime_identity,
            "model_checksum": model.model_checksum,
            "model_semantic_checksum": model.model_semantic_checksum,
            "inference_contract_checksum": model.inference_contract_checksum,
        }

    @staticmethod
    def _serialize_prediction(prediction: PredictionRun | None) -> dict | None:
        if prediction is None:
            return None
        return {
            "id": str(prediction.id),
            "trained_model_id": str(prediction.trained_model_id),
            "prediction_start": prediction.prediction_start,
            "prediction_end": prediction.prediction_end,
            "cross_section_count": prediction.cross_section_count,
            "row_count": prediction.row_count,
        }


def _records(frame) -> list[dict]:
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def _instrument_codes(session: Session) -> dict[str, str]:
    """`instrument_id` -> `source_code`, the code the source knows a security by.

    Read from `instruments`, which is the table that owns the mapping: one row
    per stable identity. The per-snapshot roster carries the same string today,
    but it is scoped to a roster and would need a date to join on, for a value
    that does not vary by date here.
    """
    return {
        str(instrument_id): source_code
        for instrument_id, source_code in session.execute(
            select(Instrument.instrument_id, Instrument.source_code)
        ).all()
    }
