from __future__ import annotations

import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.market_data import DataSnapshot, TradingCalendar
from app.models.research import ResearchExperiment, ResearchRun, ResearchRunStatus
from app.models.task import Task, TaskStatus
from app.research.publication import ResearchArtifactPublisher
from app.research.bundle_builder import QlibDataBundleBuilder
from app.research.evaluation import (
    evaluate_cross_section,
    flatten_group_returns,
    summarize_ic,
)
from app.research.factor import calculate_momentum_scores
from app.research.labels import calculate_daily_labels, calculate_weekly_labels
from app.research.qlib_runtime import analyze_signals, read_features, runtime_identity
from app.services.calendar_port import SessionCalendarPort
from app.services.stock_pool import DEFAULT_POLICY, StockPoolError, build_stock_pool
from structlog.contextvars import bind_contextvars, unbind_contextvars

try:  # Unix only; the worker runs in a Linux container, the tests also on Windows.
    import resource
except ImportError:  # pragma: no cover — exercised by the Windows dev host
    resource = None


logger = get_logger(__name__)

# One line per N observation dates while the universe is built. The loop runs
# for every trading day in the window, so tracing it at one line each buries
# the phase boundaries that say where a slow run actually spent its time.
UNIVERSE_PROGRESS_EVERY = 25


class ResearchCancelled(RuntimeError):
    pass


class _PhaseClock:
    """Wall time per phase, so a slow run can say which step was slow."""

    def __init__(self) -> None:
        self._started = time.monotonic()

    def lap(self) -> float:
        now = time.monotonic()
        elapsed = now - self._started
        self._started = now
        return round(elapsed, 3)


def _peak_rss_bytes() -> int | None:
    """Peak resident memory for this process, from the kernel.

    `tracemalloc` reports the same number more precisely, but only by watching
    every allocation for the whole run — a heavy toll on a pass that exports
    millions of rows, and paid purely to fill in one statistic. ru_maxrss is
    free, and it counts what the OOM killer counts.

    None where the platform cannot say, which is honest: a fabricated zero
    would read as "this run used no memory".
    """
    if resource is None:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


class ResearchWorkflow:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def execute(self, run_id: uuid.UUID) -> dict:
        started = time.monotonic()
        clock = _PhaseClock()
        with self.session_factory() as session:
            run = session.get(ResearchRun, run_id)
            if run is None:
                raise ValueError(f"Research run {run_id} does not exist")
            experiment = session.get(ResearchExperiment, run.experiment_id)
            snapshot = session.get(DataSnapshot, experiment.data_snapshot_id)
            # Bound here rather than passed down: the bundle exporter and the
            # stock pool log from far below this frame, and their lines are only
            # readable if they say which run they belong to.
            bind_contextvars(research_run_id=str(run_id), experiment_id=str(experiment.id))
            try:
                run.started_at = datetime.now(timezone.utc)
                run.runtime_identity = runtime_identity()
                logger.info(
                    "research_run.started",
                    data_snapshot_id=str(snapshot.id),
                    observation_start=experiment.observation_start.isoformat(),
                    observation_end=experiment.observation_end.isoformat(),
                    lookback_days=experiment.lookback_days,
                    skip_days=experiment.skip_days,
                    runtime_identity=run.runtime_identity,
                )
                self._phase(session, run, ResearchRunStatus.WAITING_FOR_BUNDLE, clock)
                bundle = QlibDataBundleBuilder(session, get_settings()).ensure(
                    snapshot, task_id=run.task_id
                )
                run.bundle_id = bundle.id
                session.commit()
                logger.info(
                    "research_run.bundle_ready",
                    bundle_id=str(bundle.id),
                    instrument_count=bundle.instrument_count,
                    size_bytes=bundle.size_bytes,
                )
                self._check_cancel(session, run)

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
                observations = [
                    day
                    for day in calendar
                    if experiment.observation_start <= day <= experiment.observation_end
                ]
                run.total_dates = len(observations)
                self._phase(session, run, ResearchRunStatus.COMPUTING_FACTORS, clock)

                required = experiment.lookback_days + experiment.skip_days
                policy = replace(
                    DEFAULT_POLICY,
                    required_history_days=required,
                    required_bar_offsets=(required, experiment.skip_days),
                )
                calendar_port = SessionCalendarPort(session, snapshot.calendar_publication_id)
                pools = {}
                universe_rows: list[dict] = []
                exclusion_rows: list[dict] = []
                skipped_dates: list[dict] = []
                for index, day in enumerate(observations, start=1):
                    self._check_cancel(session, run)
                    try:
                        pool = build_stock_pool(
                            session, snapshot, as_of=day, calendar=calendar_port, policy=policy
                        )
                    except StockPoolError as exc:
                        skipped_dates.append({"date": day.isoformat(), "reason": str(exc)})
                        continue
                    pools[day] = pool
                    universe_rows.extend(
                        {
                            "observation_date": day,
                            "instrument_id": str(member.instrument_id),
                            "symbol": member.symbol,
                            "average_turnover": float(member.average_turnover),
                            "policy_fingerprint": pool.policy_fingerprint,
                        }
                        for member in pool.members
                    )
                    exclusion_rows.extend(
                        {
                            "observation_date": day,
                            "instrument_id": str(item.instrument_id),
                            "symbol": item.symbol,
                            "reasons": [reason.value for reason in item.reasons],
                        }
                        for item in pool.exclusions
                    )
                    run.current_date = day
                    run.processed_dates = index
                    session.commit()
                    if index % UNIVERSE_PROGRESS_EVERY == 0 or index == len(observations):
                        logger.info(
                            "research_run.universe_progress",
                            processed_dates=index,
                            total_dates=len(observations),
                            observation_date=day.isoformat(),
                            members=len(pool.members),
                        )
                    else:
                        logger.debug(
                            "research_run.universe_date",
                            observation_date=day.isoformat(),
                            members=len(pool.members),
                            exclusions=len(pool.exclusions),
                        )

                logger.info(
                    "research_run.universe_built",
                    priced_dates=len(pools),
                    skipped_dates=len(skipped_dates),
                    universe_rows=len(universe_rows),
                    exclusion_rows=len(exclusion_rows),
                )
                if not pools or not any(pool.members for pool in pools.values()):
                    raise RuntimeError("No research universe could be built in the requested range")

                expression = (
                    f"Ref($close, {experiment.skip_days})/"
                    f"Ref($close, {required})-1"
                )
                bundle_path = get_settings().qlib_data_dir.resolve() / bundle.relative_path
                qlib_frame = read_features(
                    bundle_path,
                    instruments="all",
                    fields=["$close", "$open", expression],
                    start=calendar[0].isoformat(),
                    end=calendar[-1].isoformat(),
                )
                closes = self._matrix(qlib_frame, "$close")
                opens = self._matrix(qlib_frame, "$open")
                qlib_scores = self._matrix(qlib_frame, expression)
                logger.info(
                    "research_run.features_read",
                    expression=expression,
                    rows=len(qlib_frame),
                    instruments=closes.shape[1],
                    trading_days=closes.shape[0],
                )

                score_frames: list[pd.DataFrame] = []
                for day, pool in pools.items():
                    members = [str(member.instrument_id) for member in pool.members]
                    available = [item for item in members if item in closes.columns]
                    if not available:
                        continue
                    scored = calculate_momentum_scores(
                        closes[available],
                        observation_dates=[day],
                        lookback_days=experiment.lookback_days,
                        skip_days=experiment.skip_days,
                    )
                    if scored.empty:
                        continue
                    raw = qlib_scores.loc[day]
                    scored["raw_score"] = scored["instrument_id"].map(raw.to_dict())
                    valid = scored["raw_score"].notna() & scored["factor_reason"].isna()
                    ranked = scored.loc[valid, "raw_score"].rank(method="average")
                    scored.loc[valid, "average_rank"] = ranked
                    scored.loc[valid, "rank_percentile"] = ranked / len(ranked)
                    score_frames.append(scored)
                    logger.debug(
                        "research_run.scored_date",
                        observation_date=day.isoformat(),
                        candidates=len(available),
                        valid=int(valid.sum()),
                    )
                scores = pd.concat(score_frames, ignore_index=True) if score_frames else pd.DataFrame()
                logger.info(
                    "research_run.scores_built",
                    scored_dates=len(score_frames),
                    score_rows=len(scores),
                )

                self._phase(session, run, ResearchRunStatus.COMPUTING_LABELS, clock)
                daily_labels = calculate_daily_labels(opens, observation_dates=list(pools))
                weekly_observations = self._weekly_observations(list(pools))
                weekly_labels = calculate_weekly_labels(
                    opens, weekly_observation_dates=weekly_observations
                )
                universe_keys = pd.DataFrame(universe_rows)[
                    ["observation_date", "instrument_id"]
                ]
                daily_labels = daily_labels.merge(
                    universe_keys, on=["observation_date", "instrument_id"], how="inner"
                )
                weekly_labels = weekly_labels.merge(
                    universe_keys, on=["observation_date", "instrument_id"], how="inner"
                )
                logger.info(
                    "research_run.labels_built",
                    daily_label_rows=len(daily_labels),
                    weekly_label_rows=len(weekly_labels),
                    weekly_observations=len(weekly_observations),
                )

                self._phase(session, run, ResearchRunStatus.EVALUATING, clock)
                qlib_signal_analysis = self._qlib_signal_analysis(scores, daily_labels)
                daily_metrics = self._metrics(scores, daily_labels, pools, frequency="daily")
                weekly_metrics = self._metrics(scores, weekly_labels, pools, frequency="weekly")
                valid_daily = [
                    item
                    for item in daily_metrics
                    if item["factor_coverage"] >= 0.90 and item["valid_factor_count"] >= 100
                ]
                logger.info(
                    "research_run.evaluated",
                    daily_cross_sections=len(daily_metrics),
                    weekly_cross_sections=len(weekly_metrics),
                    valid_daily_cross_sections=len(valid_daily),
                )
                if not valid_daily:
                    raise RuntimeError("No valid daily factor cross-section")
                ic_summary = summarize_ic([item["ic"] for item in weekly_metrics])
                rank_ic_summary = summarize_ic([item["rank_ic"] for item in weekly_metrics])
                warnings = [
                    {"code": "free_data_limit", "detail": "J-Quants Free history limits inference"}
                ]
                if ic_summary.count < 2:
                    warnings.append({"code": "insufficient_weekly_observations"})
                if skipped_dates:
                    warnings.append({"code": "trimmed_observation_dates", "count": len(skipped_dates)})
                summary = {
                    "daily_cross_sections": len(daily_metrics),
                    "weekly_cross_sections": len(weekly_metrics),
                    "weekly_ic_mean": ic_summary.mean,
                    "weekly_icir": ic_summary.icir,
                    "weekly_rank_ic_mean": rank_ic_summary.mean,
                    "weekly_rank_icir": rank_ic_summary.icir,
                    "effective_factor_start": min(pools).isoformat() if pools else None,
                    "effective_factor_end": max(pools).isoformat() if pools else None,
                }

                self._phase(session, run, ResearchRunStatus.PUBLISHING, clock)
                labels = pd.concat([daily_labels, weekly_labels], ignore_index=True)
                publisher = ResearchArtifactPublisher(session, get_settings().research_artifact_dir)
                # Step 1+2: stage the bytes, then commit every row that describes
                # them — including the run's own terminal state — in one
                # transaction. The directory is still in staging at this point;
                # `readable_artifact` is what keeps a reader from opening it
                # before step 3 puts it in place.
                prepared = publisher.prepare(
                    run,
                    tables={
                        "universes": pd.DataFrame(universe_rows),
                        "exclusions": pd.DataFrame(exclusion_rows),
                        "scores": scores,
                        "labels": labels,
                        "daily_metrics": pd.DataFrame(daily_metrics),
                        "weekly_metrics": pd.DataFrame(weekly_metrics),
                        "qlib_daily_signal_analysis": qlib_signal_analysis,
                    },
                    summary=summary,
                    warnings=warnings,
                    runtime_identity=run.runtime_identity,
                )
                session.add(publisher.artifact_row(prepared))
                run.status = ResearchRunStatus.SUCCEEDED
                run.warnings = warnings
                run.summary = summary
                run.artifact_size_bytes = prepared.size_bytes
                run.elapsed_seconds = int(time.monotonic() - started)
                run.peak_memory_bytes = _peak_rss_bytes()
                run.finished_at = datetime.now(timezone.utc)
                session.commit()

                # Step 3+4. A crash here leaves a `prepared` publication that
                # `recover_publications` finishes on the next worker start.
                publisher.commit(prepared)
                logger.info(
                    "research_run.artifact_published",
                    relative_path=prepared.relative_path,
                    size_bytes=prepared.size_bytes,
                    logical_checksum=prepared.logical_checksum,
                )
                logger.info(
                    "research_run.succeeded",
                    elapsed_seconds=run.elapsed_seconds,
                    peak_memory_bytes=run.peak_memory_bytes,
                    artifact_size_bytes=run.artifact_size_bytes,
                    weekly_ic_mean=summary["weekly_ic_mean"],
                    weekly_rank_ic_mean=summary["weekly_rank_ic_mean"],
                    warnings=[item["code"] for item in warnings],
                )
                return summary
            except ResearchCancelled:
                self._cancel(session, run)
                logger.info("research_run.cancelled", elapsed_seconds=round(time.monotonic() - started, 3))
                return {"cancelled": True}
            except Exception as exc:
                session.rollback()
                run = session.get(ResearchRun, run_id)
                run.status = ResearchRunStatus.FAILED
                run.error_code = "research_failed"
                run.error_summary = str(exc)
                run.finished_at = datetime.now(timezone.utc)
                session.commit()
                logger.exception(
                    "research_run.failed",
                    phase=run.status.value,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
                raise
            finally:
                unbind_contextvars("research_run_id", "experiment_id")

    @staticmethod
    def _phase(
        session: Session, run: ResearchRun, status: ResearchRunStatus, clock: _PhaseClock
    ) -> None:
        previous = run.status
        run.status = status
        session.commit()
        logger.info(
            "research_run.phase",
            phase=status.value,
            previous_phase=previous.value,
            previous_seconds=clock.lap(),
        )

    @staticmethod
    def _check_cancel(session: Session, run: ResearchRun) -> None:
        session.refresh(run)
        if run.cancel_requested:
            raise ResearchCancelled()

    @staticmethod
    def _cancel(session: Session, run: ResearchRun) -> None:
        now = datetime.now(timezone.utc)
        run.status = ResearchRunStatus.CANCELLED
        run.finished_at = now
        task = session.get(Task, run.task_id)
        task.status = TaskStatus.CANCELLED
        task.finished_at = now
        session.commit()

    @staticmethod
    def _matrix(frame: pd.DataFrame, field: str) -> pd.DataFrame:
        matrix = frame[field].unstack(level="instrument")
        matrix.index = pd.Index(item.date() for item in matrix.index)
        matrix.columns = matrix.columns.map(str)
        return matrix

    @staticmethod
    def _weekly_observations(observations: list) -> list:
        by_week = {}
        for day in observations:
            by_week[day.isocalendar()[:2]] = day
        return sorted(by_week.values())

    @staticmethod
    def _qlib_signal_analysis(scores: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
        paired = scores[["observation_date", "instrument_id", "raw_score"]].merge(
            labels[["observation_date", "instrument_id", "label"]],
            on=["observation_date", "instrument_id"],
            how="inner",
        )
        paired = paired.dropna(subset=["raw_score", "label"])
        index = pd.MultiIndex.from_arrays(
            [pd.to_datetime(paired["observation_date"]), paired["instrument_id"]],
            names=["datetime", "instrument"],
        )
        analysis = analyze_signals(
            pd.DataFrame({"score": paired["raw_score"].to_numpy()}, index=index),
            pd.DataFrame({"label": paired["label"].to_numpy()}, index=index),
        )
        return pd.DataFrame(
            {
                "observation_date": analysis["ic"].index,
                "ic": analysis["ic"].to_numpy(),
                "rank_ic": analysis["rank_ic"].reindex(analysis["ic"].index).to_numpy(),
            }
        )

    @staticmethod
    def _metrics(scores, labels, pools, *, frequency: str) -> list[dict]:
        rows = []
        if scores.empty:
            return rows
        for day in sorted(set(labels["observation_date"])):
            if day not in pools:
                continue
            day_scores = scores[scores["observation_date"] == day]
            day_labels = labels[labels["observation_date"] == day]
            result = evaluate_cross_section(
                day_scores, day_labels, universe_size=len(pools[day].members)
            )
            row = asdict(result)
            row.update(flatten_group_returns(row.pop("group_returns")))
            row["unavailable_reasons"] = list(result.unavailable_reasons)
            row.update({"observation_date": day, "frequency": frequency})
            rows.append(row)
        return rows
