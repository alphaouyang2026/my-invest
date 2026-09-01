"""One model run, from a queued row to a committed artifact.

Worker-only, like ticket 06's factor workflow: the API process never reaches
this module and never initialises Qlib.

The order of the first two steps is deliberate. The execution spec is compiled
and checked *before* the bundle is touched, so a run whose stored definition the
current code cannot reproduce fails without initialising Qlib, without building
a data bundle, and without leaving anything behind to clean up.

Cancellation is best-effort by design. The flag is read every
`CANCEL_CHECK_EVERY` boosting rounds and at each phase boundary; if training and
publishing finish before a request is observed, the run is allowed to succeed
and its artifacts stand. The API therefore has to distinguish "cancellation
requested" from "run cancelled", and never show the first as the second.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from structlog.contextvars import bind_contextvars, unbind_contextvars

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.market_data import DataSnapshot, TradingCalendar
from app.models.research import (
    PredictionRun,
    ResearchExperiment,
    ResearchRun,
    ResearchRunStatus,
    TrainedModel,
)
from app.models.task import Task, TaskStatus
from app.research.bundle_builder import QlibDataBundleBuilder
from app.research.dataset import build_dataset, missing_rate_drift
from app.research.execution_spec import ExecutionDefinitionUnavailable, compile_execution_spec
from app.research.lgbm import (
    CancellableLGBModel,
    TrainingCancelled,
    is_constant_model,
    model_checksums,
)
from app.research.model_evaluation import (
    evaluate_segment,
    feature_importance_frame,
    prediction_frame,
    score_frame,
    summarize_segment,
    training_curve_frame,
)
from app.research.publication import ResearchArtifactPublisher
from app.research.qlib_runtime import run_signal_analysis, runtime_identity
from app.services.calendar_port import SessionCalendarPort
from app.services.stock_pool import DEFAULT_POLICY, StockPoolError, build_stock_pool

logger = get_logger(__name__)

MOMENTUM_SOURCE = "momentum_6_1"
MODEL_SOURCE = "lightgbm"


class ModelRunFailed(RuntimeError):
    """A run that cannot produce a meaningful model or evaluation."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class ModelResearchWorkflow:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def execute(self, run_id: uuid.UUID) -> dict:
        started = time.monotonic()
        with self.session_factory() as session:
            run = session.get(ResearchRun, run_id)
            if run is None:
                raise ValueError(f"Research run {run_id} does not exist")
            experiment = session.get(ResearchExperiment, run.experiment_id)
            snapshot = session.get(DataSnapshot, experiment.data_snapshot_id)
            bind_contextvars(research_run_id=str(run_id), experiment_id=str(experiment.id))
            try:
                run.started_at = datetime.now(timezone.utc)
                run.runtime_identity = runtime_identity()
                session.commit()

                # Before Qlib, before the bundle, before anything that would
                # need undoing.
                spec = compile_execution_spec(experiment)

                self._phase(session, run, ResearchRunStatus.WAITING_FOR_BUNDLE)
                bundle = QlibDataBundleBuilder(session, get_settings()).ensure(
                    snapshot, task_id=run.task_id
                )
                run.bundle_id = bundle.id
                session.commit()
                self._require_bundle_fields(bundle, spec)
                self._check_cancel(session, run)

                calendar, weekly = self._calendar(session, snapshot, spec)
                self._phase(session, run, ResearchRunStatus.COMPUTING_FACTORS)
                universe, universe_sizes = self._build_universe(
                    session, run, snapshot, spec, weekly
                )

                self._phase(session, run, ResearchRunStatus.COMPUTING_LABELS)
                bundle_path = get_settings().qlib_data_dir.resolve() / bundle.relative_path
                assembled = build_dataset(
                    bundle_path=bundle_path,
                    spec=spec,
                    universe=universe,
                    weekly_observations=weekly,
                    calendar=calendar,
                )

                self._phase(session, run, ResearchRunStatus.TRAINING)
                model, evals_result = self._train(session, run, spec, assembled)

                self._phase(session, run, ResearchRunStatus.PREDICTING)
                predictions, analysis = self._predict(model, assembled, spec)

                self._phase(session, run, ResearchRunStatus.EVALUATING)
                results = self._evaluate(
                    predictions=predictions,
                    assembled=assembled,
                    spec=spec,
                    universe_sizes=universe_sizes,
                    bundle_path=bundle_path,
                    calendar=calendar,
                    weekly=weekly,
                )
                results["qlib_signal_analysis"] = _qlib_analysis_frame(analysis)

                self._phase(session, run, ResearchRunStatus.PUBLISHING)
                return self._publish(
                    session=session,
                    run=run,
                    experiment=experiment,
                    spec=spec,
                    bundle=bundle,
                    model=model,
                    assembled=assembled,
                    evals_result=evals_result,
                    results=results,
                    started=started,
                )
            except TrainingCancelled:
                self._cancel(session, run)
                return {"cancelled": True}
            except (ExecutionDefinitionUnavailable, ModelRunFailed) as exc:
                self._fail(session, run_id, exc.error_code, str(exc))
                raise
            except Exception as exc:
                self._fail(session, run_id, "model_research_failed", str(exc))
                raise
            finally:
                unbind_contextvars("research_run_id", "experiment_id")

    # -- phases ------------------------------------------------------------

    def _calendar(self, session, snapshot, spec):
        calendar = list(
            session.scalars(
                select(TradingCalendar.trade_date)
                .where(
                    TradingCalendar.publication_id == snapshot.calendar_publication_id,
                    TradingCalendar.is_open.is_(True),
                )
                .order_by(TradingCalendar.trade_date)
            ).all()
        )
        # The last open session of each ISO week, which is what "weekly
        # rebalance" means on a calendar with holidays — not "every Friday".
        by_week: dict[tuple[int, int], object] = {}
        for day in calendar:
            if spec.split.train.start <= day <= spec.split.test.end:
                by_week[day.isocalendar()[:2]] = day
        return calendar, sorted(by_week.values())

    def _build_universe(self, session, run, snapshot, spec, weekly):
        policy = replace(
            DEFAULT_POLICY,
            required_history_days=spec.required_history_days,
            required_bar_offsets=(spec.required_history_days, 21),
        )
        port = SessionCalendarPort(session, snapshot.calendar_publication_id)
        universe: dict = {}
        sizes: dict = {}
        run.total_dates = len(weekly)
        for index, day in enumerate(weekly, start=1):
            self._check_cancel(session, run)
            try:
                pool = build_stock_pool(
                    session, snapshot, as_of=day, calendar=port, policy=policy
                )
            except StockPoolError as exc:
                logger.info("model_run.universe_skipped", date=day.isoformat(), reason=str(exc))
                continue
            universe[day] = {str(member.instrument_id) for member in pool.members}
            sizes[day] = len(pool.members)
            run.current_date = day
            run.processed_dates = index
            session.commit()
        if not universe:
            raise ModelRunFailed("no_training_samples", "No research universe in the requested range")
        return universe, sizes

    def _train(self, session, run, spec, assembled):
        train_rows = assembled.dataset.prepare("train", col_set=["feature", "label"])
        valid_rows = assembled.dataset.prepare("valid", col_set=["feature", "label"])
        if train_rows.empty:
            raise ModelRunFailed("no_training_samples", "The train segment has no usable rows")
        if valid_rows.empty:
            # Without a validation segment there is no early stopping, and
            # therefore no basis for the round count the model ends up with.
            raise ModelRunFailed("no_validation_samples", "The valid segment has no usable rows")

        params = dict(spec.model_params)
        num_boost_round = params.pop("num_boost_round")
        early_stopping_rounds = params.pop("early_stopping_rounds")
        evals_result: dict = {}
        model = CancellableLGBModel(
            loss=params.pop("objective"),
            early_stopping_rounds=early_stopping_rounds,
            num_boost_round=num_boost_round,
            should_cancel=lambda: self._cancel_requested(session, run),
            num_threads=get_settings().qlib_threads,
            **params,
        )
        model.fit(assembled.dataset, evals_result=evals_result, verbose_eval=0)
        logger.info(
            "model_run.trained",
            best_iteration=model.model.best_iteration,
            trees=model.model.num_trees(),
        )
        # Best-effort cancellation still has to be honoured if the request
        # arrived between two checks: training finished, but nothing has been
        # published yet, so there is nothing to undo.
        if self._cancel_requested(session, run):
            raise TrainingCancelled("Cancellation observed after training")
        return model, evals_result

    def _predict(self, model, assembled, spec) -> tuple[dict[str, pd.Series], dict]:
        """Test scores come from `SignalRecord`; train and valid from `predict`.

        Not two ways of doing the same thing. `SignalRecord` is the Qlib record
        that `SigAnaRecord` consumes, and running it is what proves the stack is
        actually wired together rather than merely imported. Its scope is the
        test segment by construction, so the other two segments — which exist to
        show the train/test gap, not to produce a signal — go through the plain
        model interface.

        Taking the test scores from the record rather than predicting a second
        time keeps one authoritative set: two independent inference passes could
        drift and nothing would notice.
        """
        analysis = run_signal_analysis(model, assembled.dataset)
        test_scores = analysis["predictions"].iloc[:, 0]
        predictions = {
            segment.name: model.predict(assembled.dataset, segment=segment.name)
            for segment in spec.split.segments
            if segment.name != "test"
        }
        predictions["test"] = test_scores
        return predictions, analysis

    def _evaluate(self, *, predictions, assembled, spec, universe_sizes, bundle_path, calendar, weekly):
        # Keep invalid and not-yet-mature labels for prediction status. The
        # evaluator filters `label.isna()` itself, so these rows remain visible
        # without contaminating IC or group-return summaries.
        labels = assembled.labels.copy()

        momentum = self._momentum_scores(bundle_path, spec, calendar, weekly, universe_sizes)

        metrics: list[pd.DataFrame] = []
        summaries: list[dict] = []
        test_scores = None
        for segment in spec.split.segments:
            scores = score_frame(predictions[segment.name])
            if segment.name == "test":
                test_scores = scores
            for source, frame in ((MODEL_SOURCE, scores), (MOMENTUM_SOURCE, momentum)):
                window = frame[
                    frame["observation_date"].between(segment.start, segment.end)
                ]
                segment_metrics = evaluate_segment(
                    window,
                    labels,
                    universe_sizes=universe_sizes,
                    segment=segment.name,
                    source=source,
                )
                metrics.append(segment_metrics)
                summaries.append(
                    summarize_segment(segment_metrics, segment=segment.name, source=source)
                )
        return {
            "metrics": pd.concat(metrics, ignore_index=True) if metrics else pd.DataFrame(),
            "summaries": summaries,
            "test_scores": test_scores if test_scores is not None else pd.DataFrame(),
            "labels": labels,
        }

    def _momentum_scores(self, bundle_path, spec, calendar, weekly, universe_sizes) -> pd.DataFrame:
        """The 6-1 control, on the same cross-sections as the model.

        Read here rather than reused from a ticket 06 run: the comparison
        requires both curves to come from the same universe and the same rows,
        and a stored 06 artifact was produced against its own.
        """
        from app.research.qlib_runtime import read_features

        expression = "Ref($close, 21)/Ref($close, 147)-1"
        frame = read_features(
            bundle_path,
            instruments="all",
            fields=[expression],
            start=calendar[0].isoformat(),
            end=calendar[-1].isoformat(),
        )
        series = frame[expression].swaplevel().sort_index()
        series.index = series.index.set_names(["datetime", "instrument"])
        wanted = pd.to_datetime(pd.Index(weekly))
        series = series[series.index.get_level_values("datetime").isin(wanted)]
        return score_frame(series.dropna())

    def _publish(self, *, session, run, experiment, spec, bundle, model, assembled, evals_result, results, started):
        booster = model.model
        model_text = booster.model_to_string()
        file_checksum, semantic_checksum = model_checksums(model_text)

        constant, reasons = is_constant_model(booster, results["test_scores"].get("raw_score"))
        if constant:
            raise ModelRunFailed("model_no_useful_iterations", "; ".join(reasons))

        test_summary = next(
            item
            for item in results["summaries"]
            if item["segment"] == "test" and item["source"] == MODEL_SOURCE
        )
        if not test_summary["observations"]:
            raise ModelRunFailed(
                "no_valid_test_cross_section", "No test cross-section passed the coverage thresholds"
            )

        warnings = [{"code": "free_data_limit", "detail": "J-Quants Free history limits inference"}]
        if test_summary["observations"] < 2:
            warnings.append({"code": "insufficient_test_observations", "count": test_summary["observations"]})
        if spec.feature_set.maturity == "experimental":
            warnings.append(
                {
                    "code": "experimental_feature_set",
                    "detail": f"{spec.feature_set.name} uses short history; results are exploratory",
                }
            )
        drift = missing_rate_drift(assembled.missing_rate)
        if spec.feature_set.maturity == "experimental" and not drift.empty:
            worst = drift.sort_values("drift", ascending=False).iloc[0]
            warnings.append(
                {
                    "code": "high_test_missing_rate",
                    "detail": (
                        f"{len(drift)} feature(s) drifted by more than 10 percentage points; "
                        f"worst={worst['feature_name']} ({worst['drift']:.1%})"
                    ),
                    "count": len(drift),
                    "max_drift": float(worst["drift"]),
                }
            )

        importance = feature_importance_frame(booster, list(spec.feature_set.column_names))
        summary = {
            "segments": results["summaries"],
            "test_observations": test_summary["observations"],
            "best_iteration": booster.best_iteration,
            "zero_gain_features": int((importance["gain"] == 0).sum()),
            "feature_set": spec.feature_set.name,
        }

        trained_model_id = uuid.uuid4()
        published_scores = prediction_frame(
            results["test_scores"],
            results["labels"],
            trained_model_id=trained_model_id,
            data_snapshot_id=experiment.data_snapshot_id,
        )

        publisher = ResearchArtifactPublisher(session, get_settings().research_artifact_dir)
        prepared = publisher.prepare(
            run,
            tables={
                "predictions": published_scores,
                "metrics": results["metrics"],
                "feature_importance": importance,
                "feature_missing_rate": assembled.missing_rate,
                "feature_group_anomalies": assembled.feature_group_anomalies,
                "training_curve": training_curve_frame(evals_result),
                "labels": results["labels"],
                "qlib_signal_analysis": results["qlib_signal_analysis"],
            },
            summary=summary,
            warnings=warnings,
            runtime_identity=run.runtime_identity,
            extra_files={"model.txt": model_text.encode("utf-8")},
        )
        session.add(publisher.artifact_row(prepared))
        trained = TrainedModel(
            id=trained_model_id,
            research_run_id=run.id,
            experiment_id=experiment.id,
            data_snapshot_id=experiment.data_snapshot_id,
            bundle_id=bundle.id,
            feature_set_name=spec.feature_set.name,
            feature_set_version=spec.feature_set.version,
            label_definition=spec.label_definition,
            train_start=spec.split.train.start,
            train_end=spec.split.train.end,
            valid_start=spec.split.valid.start,
            valid_end=spec.split.valid.end,
            test_start=spec.split.test.start,
            test_end=spec.split.test.end,
            fit_start=spec.fit_start,
            fit_end=spec.fit_end,
            model_params={**spec.model_params, "num_threads": get_settings().qlib_threads},
            seed=spec.seed,
            best_iteration=booster.best_iteration,
            runtime_identity=run.runtime_identity,
            inference_contract=spec.inference_contract,
            inference_contract_checksum=_checksum(spec.inference_contract),
            relative_path=f"{prepared.relative_path}/model.txt",
            model_checksum=file_checksum,
            model_semantic_checksum=semantic_checksum,
        )
        session.add(trained)
        session.flush()
        scores = published_scores
        session.add(
            PredictionRun(
                research_run_id=run.id,
                trained_model_id=trained.id,
                data_snapshot_id=experiment.data_snapshot_id,
                prediction_start=spec.split.test.start,
                prediction_end=spec.split.test.end,
                cross_section_count=int(scores["observation_date"].nunique()) if not scores.empty else 0,
                row_count=len(scores),
                artifact_relative_path=f"{prepared.relative_path}/predictions.parquet",
            )
        )
        run.status = ResearchRunStatus.SUCCEEDED
        run.warnings = warnings
        run.summary = summary
        run.artifact_size_bytes = prepared.size_bytes
        run.elapsed_seconds = int(time.monotonic() - started)
        run.finished_at = datetime.now(timezone.utc)
        session.commit()

        publisher.commit(prepared)
        logger.info("model_run.succeeded", elapsed_seconds=run.elapsed_seconds, **{
            "test_observations": test_summary["observations"],
            "best_iteration": booster.best_iteration,
        })
        return summary

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _require_bundle_fields(bundle, spec) -> None:
        """A missing field is a hard failure, never a silently NaN column."""
        available = {str(field).lower() for field in (bundle.manifest or {}).get("fields", [])}
        missing = sorted(set(spec.feature_set.required_fields) - available)
        if missing:
            raise ModelRunFailed(
                "missing_bundle_field",
                f"Bundle {bundle.id} lacks field(s) {missing} required by {spec.feature_set.name}",
            )

    @staticmethod
    def _phase(session: Session, run: ResearchRun, status: ResearchRunStatus) -> None:
        previous = run.status
        run.status = status
        session.commit()
        logger.info("model_run.phase", phase=status.value, previous_phase=previous.value)

    @staticmethod
    def _cancel_requested(session: Session, run: ResearchRun) -> bool:
        session.refresh(run)
        return bool(run.cancel_requested)

    def _check_cancel(self, session: Session, run: ResearchRun) -> None:
        if self._cancel_requested(session, run):
            raise TrainingCancelled("Cancellation requested")

    @staticmethod
    def _cancel(session: Session, run: ResearchRun) -> None:
        session.rollback()
        run = session.get(ResearchRun, run.id)
        now = datetime.now(timezone.utc)
        run.status = ResearchRunStatus.CANCELLED
        run.finished_at = now
        task = session.get(Task, run.task_id)
        task.status = TaskStatus.CANCELLED
        task.finished_at = now
        session.commit()
        logger.info("model_run.cancelled")

    @staticmethod
    def _fail(session: Session, run_id: uuid.UUID, error_code: str, message: str) -> None:
        session.rollback()
        run = session.get(ResearchRun, run_id)
        run.status = ResearchRunStatus.FAILED
        run.error_code = error_code
        run.error_summary = message
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        logger.warning("model_run.failed", error_code=error_code, error_summary=message)


def _checksum(payload: dict) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _qlib_analysis_frame(analysis: dict) -> pd.DataFrame:
    """`SigAnaRecord`'s own IC series, published beside the local one.

    Kept as a separate table rather than merged into `metrics`: it is Qlib's
    figure over the test segment, and the integration test asserts the two agree
    within tolerance. Merging them would make a disagreement invisible, which is
    the one thing this table is for.
    """
    ic = analysis.get("ic")
    if ic is None or len(ic) == 0:
        return pd.DataFrame(columns=["observation_date", "ic", "rank_ic"])
    rank_ic = analysis.get("rank_ic")
    return pd.DataFrame(
        {
            "observation_date": pd.to_datetime(ic.index).date,
            "ic": ic.to_numpy(),
            "rank_ic": rank_ic.reindex(ic.index).to_numpy() if rank_ic is not None else None,
        }
    )
