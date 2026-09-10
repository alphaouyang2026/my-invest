"""Direct DataSnapshot -> Qlib Alpha -> LightGBM experiment.

The one place a model is trained and scored against a snapshot. The command line
calls it for a single run; an experiment search calls it once per trial, so a
candidate's leaderboard position and a later run are the same code rather than
two implementations that agree for now.

Provider build and delete are here too, and are synchronous and intentionally do
not call one another implicitly: a prediction never builds a provider behind the
caller's back.

No task, bundle ORM row, recorder, publication, or artifact is created here.
Features live in a checksummed cache owned by `feature_shard_cache`.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Literal

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.experiments.artifact_cache import (
    digest, exclusive_lock, read_json, seal, verified, write_json, write_parquet,
)
from app.experiments.feature_shard_cache import (
    DEFAULT_FEATURE_BATCH_SIZE,
    DEFAULT_QLIB_KERNELS,
    DEFAULT_SCAN_BATCH_ROWS,
    DateSpan,
    FeatureCacheError,
    FeatureCacheSpec,
    MatrixSegment,
    assemble_sharded_dataset,
    mature_label_dates,
)
from app.experiments.booster_training import (
    STOP_METRICS,
    BoosterTrainingError,
    TrainingMatrix,
    train_booster,
)
from app.models.market_data import DataSnapshot
from app.research.day_provider import (
    BuildDayProviderResult,
    DayProviderError,
    DayProviderRef,
    build_day_provider,
    delete_day_provider,
    open_day_provider,
)
from app.research.feature_sets import UnknownFeatureSetError, get_feature_set
from app.research.lgbm import is_constant_model
from app.research.model_definition import (
    DEFAULT_SEED,
    MIN_COVERAGE,
    MIN_VALID_SECURITIES,
    resolve_model_params,
)
from app.research.model_evaluation import (
    evaluate_segment,
    feature_importance_frame,
    score_frame,
    summarize_segment,
)
from app.services.calendar_port import CalendarCoverageError, SessionCalendarPort
from app.services.stock_pool import DEFAULT_POLICY, StockPoolError, build_stock_pool

DEFAULT_PROVIDER_ROOT = Path("var/qlib-direct-providers")
FEATURE_SET_ALIASES = {
    "alpha158": "alpha158_jp_v1",
    "alpha360": "alpha360_jp_v1",
}
DEFAULT_LABEL_HORIZON = 5
DEFAULT_ROLLING_STEP = 20
DEFAULT_STOP_METRIC = "l2"
#: Every fold trains from `train.start`. The alternative is a fixed-length window
#: that slides with the fold, which is what a search means by "252".
EXPANDING = "expanding"


class DirectExperimentError(RuntimeError):
    exit_code = 2


class DirectExperimentConfigError(DirectExperimentError):
    pass


class DirectProviderError(DirectExperimentError):
    pass


class DirectExperimentDataError(DirectExperimentError):
    pass


class DirectExperimentTrainingError(DirectExperimentError):
    exit_code = 1


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise DirectExperimentConfigError(
                f"Date range starts after it ends: {self.start}..{self.end}"
            )


@dataclass(frozen=True)
class DirectPredictionConfig:
    """What one direct run is.

    The last three fields are the ones a search varies. They default to the
    baseline so an unqualified run still means something, but they exist so a
    searched answer can be executed here rather than only described.
    """

    snapshot_id: uuid.UUID
    feature_set: Literal["alpha158", "alpha360"]
    train: DateRange
    valid: DateRange
    test: DateRange
    provider_root: Path = DEFAULT_PROVIDER_ROOT
    seed: int = DEFAULT_SEED
    num_threads: int = 2
    label_horizon: int = DEFAULT_LABEL_HORIZON
    rolling_step: int = DEFAULT_ROLLING_STEP
    model_params: Mapping[str, Any] | None = None
    stop_metric: Literal["l2", "rank_ic"] = DEFAULT_STOP_METRIC
    train_window: int | Literal["expanding"] = EXPANDING
    purge_horizon: int | None = None
    feature_batch_size: int = DEFAULT_FEATURE_BATCH_SIZE
    qlib_kernels: int = DEFAULT_QLIB_KERNELS
    scan_batch_rows: int = DEFAULT_SCAN_BATCH_ROWS

    @property
    def effective_purge_horizon(self) -> int:
        """Trading days of label lookahead the segment seams are purged by.

        Normally the label horizon: a 20-day label cannot be known until 21
        sessions later. A caller comparing several horizons over one evaluation
        window purges every one of them by the longest, so that the horizons are
        scored on identical dates and their results mean the same thing. Passing
        it explicitly is the only way that comparison and this entry point can
        produce the same folds.
        """
        return self.label_horizon if self.purge_horizon is None else self.purge_horizon


@dataclass(frozen=True)
class DirectExperimentResult:
    summary: dict[str, object]
    daily_ic: pd.DataFrame
    predictions: pd.DataFrame
    feature_importance: pd.DataFrame


@dataclass(frozen=True)
class _RollingFold:
    number: int
    train: DateRange
    valid: DateRange
    test: DateRange


def run_direct_prediction(
    session: Session,
    config: DirectPredictionConfig,
    *,
    cache_root: Path | None = None,
    artifact_dir: Path | None = None,
) -> DirectExperimentResult:
    """Train and score one rolling experiment against a snapshot.

    `cache_root` names where computed features live between calls; it defaults
    beside the provider, so repeating a run costs no Qlib work and a caller
    sweeping many parameter sets over one dataset pays for it once.

    `artifact_dir`, when given, receives the per-fold boosters and learning
    curves. The command line does not ask for them -- it prints a summary -- but
    a search ranks candidates on them, and writing them here is what lets that
    search stop owning a second copy of this loop.
    """
    started = time.monotonic()
    snapshot = load_and_validate_snapshot(session, config)
    feature_set = resolve_feature_set(config.feature_set)
    calendar = SessionCalendarPort(session, snapshot.calendar_publication_id)
    folds, dates = _build_rolling_plan(snapshot, calendar, config)
    try:
        provider = open_day_provider(config.snapshot_id, config.provider_root)
    except DayProviderError as exc:
        raise DirectProviderError(str(exc)) from exc
    if str(snapshot.calendar_publication_id) != provider.manifest.calendar_publication_id:
        raise DirectProviderError("Provider calendar publication does not match DataSnapshot")
    if snapshot.bar_publish_sequence != provider.manifest.snapshot_bar_publish_sequence:
        raise DirectProviderError("Provider bar publish sequence does not match DataSnapshot")

    # The pool is built for the whole span rather than for the days the folds
    # happen to touch. The cached features are filtered by it, so anything the
    # pool depends on has to be in the cache identity -- and `train_window`,
    # `purge_horizon`, `rolling_step` and the valid window all move fold days
    # without moving the span. Building the superset keeps the cache a function
    # of what identifies it, and lets runs that differ only in fold geometry
    # share one.
    artifact_cache_root = cache_root or (config.provider_root.parent / "qlib-feature-cache")
    universe, universe_sizes, symbols, warnings, policy_fingerprint = _cached_universe(
        session, snapshot, calendar, _span_dates(calendar, config), feature_set.max_window,
        cache_root=artifact_cache_root,
    )
    params = resolve_model_params(config.model_params, seed=config.seed)
    try:
        dataset = assemble_sharded_dataset(
            provider,
            feature_set,
            _cache_spec(config),
            universe,
            snapshot=snapshot,
            policy_fingerprint=policy_fingerprint,
            cache_root=artifact_cache_root,
            # The run's own calendar decides which days it needs; the cache only
            # knows which it holds. Passed to assembly rather than asked of the
            # reader afterwards, so a reader that was never checked cannot exist.
            required_days={*dates["train"], *dates["valid"], *dates["test"]},
        )
        raw_labels = dataset.raw_labels()
        outcomes: list[FoldOutcome] = []
        for fold in folds:
            outcomes.append(
                run_fold(
                    fold.number,
                    dataset.load_segment(fold.train, learning=True),
                    dataset.load_segment(fold.valid, learning=True),
                    dataset.load_segment(fold.test, learning=False),
                    raw_labels,
                    universe_sizes,
                    params=params,
                    stop_metric=config.stop_metric,
                    num_threads=config.num_threads,
                )
            )
    except FeatureCacheError as exc:
        raise DirectExperimentDataError(str(exc)) from exc

    date_to_fold: dict[date, int] = {}
    for fold, outcome in zip(folds, outcomes):
        for day in dates["fold_test"][fold.number]:
            date_to_fold[day] = fold.number
        if artifact_dir is not None:
            models = Path(artifact_dir) / "models"
            models.mkdir(parents=True, exist_ok=True)
            outcome.booster.save_model(
                str(models / f"fold-{fold.number}.txt"),
                num_iteration=outcome.booster.best_iteration,
            )

    fold_summaries = [
        dict(
            outcome.info,
            train=[fold.train.start.isoformat(), fold.train.end.isoformat()],
            valid=[fold.valid.start.isoformat(), fold.valid.end.isoformat()],
            test=[fold.test.start.isoformat(), fold.test.end.isoformat()],
        )
        for fold, outcome in zip(folds, outcomes)
    ]
    raw_predictions = pd.concat([outcome.predictions for outcome in outcomes]).sort_index()
    if raw_predictions.index.has_duplicates:
        raise DirectExperimentDataError("Rolling folds produced duplicate prediction rows")

    daily_ic = pd.concat([outcome.daily_ic for outcome in outcomes], ignore_index=True)
    daily_ic = _decorate_daily_metrics(
        daily_ic,
        provider,
        universe_sizes,
        config.label_horizon,
        snapshot_coverage_end=snapshot.coverage_end,
    )
    metrics_summary = summarize_segment(daily_ic, segment="test", source="lightgbm")
    if not metrics_summary["observations"]:
        raise DirectExperimentDataError(
            "No test cross-section produced a valid IC after coverage thresholds"
        )

    scores = score_frame(raw_predictions)
    predictions = _prediction_output(scores, raw_labels, symbols)
    predictions.insert(0, "fold", predictions["datetime"].map(date_to_fold).astype(int))
    importance = pd.concat([outcome.importance for outcome in outcomes], ignore_index=True)
    curves = pd.concat([outcome.curve for outcome in outcomes], ignore_index=True)
    if artifact_dir is not None:
        write_parquet(Path(artifact_dir) / "learning_curves.parquet", curves)

    prediction_dates = int(predictions["datetime"].nunique()) if not predictions.empty else 0
    pool_values = list(universe_sizes.values())
    summary: dict[str, object] = {
        "snapshot_id": str(snapshot.id),
        "snapshot_version": snapshot.version,
        "provider_path": str(provider.path),
        "provider_logical_checksum": provider.manifest.logical_checksum,
        "feature_cache_path": str(dataset.root),
        "feature_set": config.feature_set,
        "feature_count": len(feature_set.features),
        "feature_definition_checksum": feature_set.definition_checksum,
        "stock_pool_policy_fingerprint": policy_fingerprint,
        "label_horizon": config.label_horizon,
        "purge_horizon": config.effective_purge_horizon,
        "label_expression": (
            f"Ref($close, -{config.label_horizon + 1}) / Ref($close, -1) - 1"
        ),
        "rolling_step": config.rolling_step,
        "rolling_train_policy": config.train_window,
        "stop_metric": config.stop_metric,
        "model_params": params,
        "fold_count": len(folds),
        "folds": fold_summaries,
        "prediction_dates": len(dates["test"]),
        "valid_prediction_dates": prediction_dates,
        "min_pool_size": min(pool_values),
        "max_pool_size": max(pool_values),
        "train_rows": sum(int(item["train_rows"]) for item in fold_summaries),
        "valid_rows": sum(int(item["valid_rows"]) for item in fold_summaries),
        "test_rows": sum(int(item["test_rows"]) for item in fold_summaries),
        "best_iterations": [int(item["best_iteration"]) for item in fold_summaries],
        "test_ic_dates": int(metrics_summary["observations"]),
        "excluded_metric_dates": int(len(daily_ic) - metrics_summary["observations"]),
        "test_ic_mean": metrics_summary["ic_mean"],
        "test_icir": metrics_summary["icir"],
        "test_rank_ic_mean": metrics_summary["rank_ic_mean"],
        "test_rank_icir": metrics_summary["rank_icir"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "pool_warnings": warnings,
    }
    return DirectExperimentResult(summary, daily_ic, predictions, importance)


def load_and_validate_snapshot(session: Session, config: DirectPredictionConfig) -> DataSnapshot:
    """Resolve the snapshot a config names, rejecting anything unrunnable."""
    snapshot = session.get(DataSnapshot, config.snapshot_id)
    if snapshot is None:
        raise DirectExperimentConfigError(f"DataSnapshot {config.snapshot_id} does not exist")
    if snapshot.source != "jquants":
        raise DirectExperimentConfigError(
            f"DataSnapshot source must be 'jquants', got {snapshot.source!r}"
        )
    if not snapshot.is_backtest_eligible:
        raise DirectExperimentConfigError(f"DataSnapshot {snapshot.id} is not backtest eligible")
    if (
        not isinstance(config.num_threads, int)
        or isinstance(config.num_threads, bool)
        or config.num_threads < 1
    ):
        raise DirectExperimentConfigError("num_threads must be a positive integer")
    for name, value in (
        ("label_horizon", config.label_horizon),
        ("rolling_step", config.rolling_step),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise DirectExperimentConfigError(f"{name} must be a positive integer")
    if config.purge_horizon is not None and (
        not isinstance(config.purge_horizon, int)
        or isinstance(config.purge_horizon, bool)
        or config.purge_horizon < config.label_horizon
    ):
        raise DirectExperimentConfigError(
            "purge_horizon must be an integer at least as long as label_horizon "
            f"({config.label_horizon}), got {config.purge_horizon!r}"
        )
    if config.stop_metric not in STOP_METRICS:
        raise DirectExperimentConfigError(
            f"stop_metric must be one of {sorted(STOP_METRICS)}, got {config.stop_metric!r}"
        )
    if config.train_window != EXPANDING and (
        not isinstance(config.train_window, int)
        or isinstance(config.train_window, bool)
        or config.train_window < 1
    ):
        raise DirectExperimentConfigError(
            f"train_window must be {EXPANDING!r} or a positive integer, "
            f"got {config.train_window!r}"
        )
    try:
        resolve_model_params(config.model_params, seed=config.seed)
    except ValueError as exc:
        raise DirectExperimentConfigError(str(exc)) from exc
    return snapshot


def resolve_feature_set(alias: str):
    try:
        registered = FEATURE_SET_ALIASES[alias]
    except KeyError as exc:
        raise DirectExperimentConfigError(
            f"feature_set must be one of {sorted(FEATURE_SET_ALIASES)}, got {alias!r}"
        ) from exc
    try:
        return get_feature_set(registered)
    except UnknownFeatureSetError as exc:
        raise DirectExperimentConfigError(str(exc)) from exc


def _build_rolling_plan(snapshot, calendar, config):
    ordered = [
        config.train.start,
        config.train.end,
        config.valid.start,
        config.valid.end,
        config.test.start,
        config.test.end,
    ]
    if not (
        config.train.end < config.valid.start
        and config.valid.end < config.test.start
    ):
        raise DirectExperimentConfigError(
            "train, valid and test must be ordered and non-overlapping"
        )
    if any(day < snapshot.coverage_start or day > snapshot.coverage_end for day in ordered):
        raise DirectExperimentConfigError(
            f"All segment endpoints must lie in snapshot coverage "
            f"{snapshot.coverage_start}..{snapshot.coverage_end}"
        )
    try:
        for day in ordered:
            if not calendar.is_open(day):
                raise DirectExperimentConfigError(f"Segment endpoint {day} is not an open day")
        all_days = tuple(
            calendar.open_days_between(config.train.start, snapshot.coverage_end)
        )
        positions = {day: index for index, day in enumerate(all_days)}
        overall_test = tuple(calendar.open_days_between(config.test.start, config.test.end))
    except CalendarCoverageError as exc:
        raise DirectExperimentConfigError(str(exc)) from exc
    if not overall_test:
        raise DirectExperimentConfigError("The overall test range has no open trading day")

    def shifted(day: date, offset: int) -> date:
        index = positions[day] + offset
        if index >= len(all_days):
            raise DirectExperimentConfigError(
                f"Rolling {day} forward {offset} trading days exceeds snapshot coverage"
            )
        return all_days[index]

    label_lookahead = config.effective_purge_horizon + 1
    folds: list[_RollingFold] = []
    fold_dates: dict[int, tuple[date, ...]] = {}
    all_train: set[date] = set()
    all_valid: set[date] = set()
    for offset in range(0, len(overall_test), config.rolling_step):
        number = len(folds) + 1
        test_days = overall_test[offset : offset + config.rolling_step]
        valid_start = shifted(config.valid.start, offset)
        requested_train_end = shifted(config.train.end, offset)
        requested_valid_end = shifted(config.valid.end, offset)
        max_train_end_index = positions[valid_start] - label_lookahead - 1
        max_valid_end_index = positions[test_days[0]] - label_lookahead - 1
        train_end_index = min(positions[requested_train_end], max_train_end_index)
        valid_end_index = min(positions[requested_valid_end], max_valid_end_index)
        if train_end_index < positions[config.train.start]:
            raise DirectExperimentConfigError(
                f"Fold {number} train range is empty after {label_lookahead}-day label purge"
            )
        if valid_end_index < positions[valid_start]:
            raise DirectExperimentConfigError(
                f"Fold {number} valid range is empty after {label_lookahead}-day label purge"
            )
        train_start_index = positions[config.train.start]
        if config.train_window != EXPANDING:
            # A fixed window slides with the fold. Only the first fold can be short,
            # and a short first fold is a misconfiguration rather than a fold to
            # silently train on less history than every other fold gets.
            train_start_index = train_end_index - config.train_window + 1
            if train_start_index < positions[config.train.start]:
                available = train_end_index - positions[config.train.start] + 1
                raise DirectExperimentConfigError(
                    f"Fold {number} has {available} trading days from {config.train.start} "
                    f"to its purged train end, fewer than the {config.train_window}-day "
                    f"train_window; move train.start earlier or shorten the window"
                )
        fold = _RollingFold(
            number=number,
            train=DateRange(all_days[train_start_index], all_days[train_end_index]),
            valid=DateRange(valid_start, all_days[valid_end_index]),
            test=DateRange(test_days[0], test_days[-1]),
        )
        folds.append(fold)
        fold_dates[number] = test_days
        all_train.update(calendar.open_days_between(fold.train.start, fold.train.end))
        all_valid.update(calendar.open_days_between(fold.valid.start, fold.valid.end))
    dates = {
        "train": tuple(sorted(all_train)),
        "valid": tuple(sorted(all_valid)),
        "test": overall_test,
        "fold_test": fold_dates,
    }
    return tuple(folds), dates


def _cache_spec(config: DirectPredictionConfig) -> FeatureCacheSpec:
    """What of this run the cached features depend on.

    Not the fold geometry: the pool below is built for the whole span precisely
    so that `train_window`, `purge_horizon` and `rolling_step` cannot reach the
    cached rows without reaching this.
    """
    return FeatureCacheSpec(
        span=DateSpan(config.train.start, config.test.end),
        label_horizon=config.label_horizon,
        feature_batch_size=config.feature_batch_size,
        qlib_kernels=config.qlib_kernels,
        scan_batch_rows=config.scan_batch_rows,
    )


def _span_dates(calendar, config: DirectPredictionConfig) -> tuple[date, ...]:
    """Every open day the run could touch, whatever the folds turn out to be."""
    try:
        return tuple(calendar.open_days_between(config.train.start, config.test.end))
    except CalendarCoverageError as exc:
        raise DirectExperimentConfigError(str(exc)) from exc


def _build_universe(session, snapshot, calendar, days, max_window):
    required = max(max_window, 20)
    policy = replace(
        DEFAULT_POLICY,
        required_history_days=required,
        required_bar_offsets=tuple(sorted({required, 20, 5, 1}, reverse=True)),
    )
    if not days:
        raise DirectExperimentConfigError("The requested span contains no open trading day")
    universe: dict[date, set[str]] = {}
    sizes: dict[date, int] = {}
    fingerprint: str | None = None
    symbols: dict[str, str] = {}
    warnings: list[dict[str, object]] = []
    seen_warnings: set[str] = set()
    for day in sorted(days):
        try:
            pool = build_stock_pool(session, snapshot, as_of=day, calendar=calendar, policy=policy)
        except StockPoolError as exc:
            raise DirectExperimentDataError(f"Stock pool failed for {day}: {exc}") from exc
        if not pool.members:
            raise DirectExperimentDataError(f"Stock pool is empty for {day}")
        fingerprint = pool.policy_fingerprint
        universe[day] = {str(member.instrument_id) for member in pool.members}
        sizes[day] = len(pool.members)
        symbols.update({str(member.instrument_id): member.symbol for member in pool.members})
        for warning in pool.warnings:
            payload = {
                "date": day.isoformat(),
                "code": warning.code.value,
                "detail": warning.detail,
            }
            key = repr(payload)
            if key not in seen_warnings:
                seen_warnings.add(key)
                warnings.append(payload)
    return universe, sizes, symbols, warnings, fingerprint


def _universe_identity(snapshot, days, policy) -> str:
    """What the pool is a function of, and nothing else.

    Not the feature set that asked for it: two feature sets wanting the same
    history length want the same pool, and `required_history_days` is already
    here. Not the fold geometry either -- the pool is built for the whole span,
    so a run that only moves fold boundaries reads this one back.
    """
    return digest(
        {
            "snapshot": [str(snapshot.id), snapshot.version, snapshot.bar_publish_sequence],
            "calendar": str(snapshot.calendar_publication_id),
            "policy": asdict(policy),
            "days": [day.isoformat() for day in sorted(days)],
        }
    )


def _cached_universe(session, snapshot, calendar, days, max_window, *, cache_root: Path):
    """Build the pool once per dataset instead of once per model.

    Every trial in a search spans the same days under the same policy, and each
    day is a database round trip: rebuilding it per model cost more than the
    training it preceded. Keyed like the feature cache -- by the data, never by
    the code -- so an edit anywhere does not discard a pool whose contents did
    not change.
    """
    required = max(max_window, 20)
    policy = replace(
        DEFAULT_POLICY,
        required_history_days=required,
        required_bar_offsets=tuple(sorted({required, 20, 5, 1}, reverse=True)),
    )
    if not days:
        raise DirectExperimentConfigError("The requested span contains no open trading day")
    identity = _universe_identity(snapshot, days, policy)
    root = cache_root / f"universe-{identity[:20]}"
    with exclusive_lock(root):
        try:
            if not verified(root, identity):
                universe, sizes, symbols, warnings, fingerprint = _build_universe(
                    session, snapshot, calendar, days, max_window
                )
                write_json(root / "universe.json", {
                    "universe": {day.isoformat(): sorted(members)
                                 for day, members in universe.items()},
                    "sizes": {day.isoformat(): size for day, size in sizes.items()},
                    "symbols": symbols,
                    "warnings": warnings,
                    "policy_fingerprint": fingerprint,
                })
                seal(root, identity)
            saved = read_json(root / "universe.json")
        except (OSError, ValueError) as exc:
            raise DirectExperimentDataError(f"Stock pool cache is unusable: {exc}") from exc
    return (
        {date.fromisoformat(day): set(members) for day, members in saved["universe"].items()},
        {date.fromisoformat(day): size for day, size in saved["sizes"].items()},
        saved["symbols"],
        saved["warnings"],
        saved["policy_fingerprint"],
    )


@dataclass(frozen=True)
class FoldOutcome:
    number: int
    booster: Any
    predictions: pd.Series
    daily_ic: pd.DataFrame
    importance: pd.DataFrame
    curve: pd.DataFrame
    info: dict[str, Any]


def _as_training_matrix(segment: MatrixSegment) -> TrainingMatrix:
    """Adapt a cached segment to what the trainer takes.

    Here rather than on `MatrixSegment`, so the cache does not have to know a
    training type exists: it holds arrays, and this module knows both sides.
    """
    return TrainingMatrix(
        features=segment.features,
        labels=segment.labels,
        dates=segment.dates,
        feature_names=segment.feature_names,
    )


def run_fold(
    number: int,
    train: MatrixSegment,
    valid: MatrixSegment,
    test: MatrixSegment,
    raw_labels: pd.DataFrame,
    universe_sizes: dict[date, int],
    *,
    params: dict,
    stop_metric: str,
    num_threads: int,
) -> FoldOutcome:
    """Train one fold and score it. The only place a booster meets test features.

    Both entry points call this, so a parameter set that a search ranked and one
    that a run executes cannot quietly mean two different models.
    """
    if not len(train):
        raise DirectExperimentDataError(f"Fold {number} train segment has no usable rows")
    if not len(valid):
        raise DirectExperimentDataError(f"Fold {number} valid segment has no usable rows")
    if not len(test):
        raise DirectExperimentDataError(f"Fold {number} test segment has no feature rows")
    try:
        outcome = train_booster(
            _as_training_matrix(train),
            _as_training_matrix(valid),
            params,
            stop_metric=stop_metric,
            num_threads=num_threads,
        )
    except BoosterTrainingError as exc:
        raise DirectExperimentDataError(str(exc)) from exc
    except Exception as exc:
        raise DirectExperimentTrainingError(
            f"LightGBM training failed in fold {number}: {exc}"
        ) from exc

    booster = outcome.booster
    try:
        scores = pd.Series(
            booster.predict(test.features, num_iteration=booster.best_iteration),
            index=test.index,
            name="score",
            dtype="float64",
        )
    except Exception as exc:
        raise DirectExperimentTrainingError(
            f"LightGBM prediction failed in fold {number}: {exc}"
        ) from exc
    if scores.empty or not np.isfinite(scores.to_numpy()).all():
        raise DirectExperimentTrainingError(
            f"LightGBM produced empty or non-finite predictions in fold {number}"
        )
    constant, reasons = is_constant_model(booster, scores)
    if constant:
        raise DirectExperimentTrainingError(
            f"Constant LightGBM model in fold {number}: " + "; ".join(reasons)
        )

    daily = evaluate_segment(
        score_frame(scores),
        raw_labels,
        universe_sizes=universe_sizes,
        segment="test",
        source="lightgbm",
    )
    daily.insert(0, "fold", number)
    importance = feature_importance_frame(booster, list(test.feature_names))
    importance.insert(0, "fold", number)
    curve = outcome.curve.copy()
    curve.insert(0, "fold", number)
    info = dict(outcome.info) | {
        "fold": number,
        "train_rows": len(train),
        "valid_rows": len(valid),
        "test_rows": len(test),
        "unique_scores": int(scores.nunique()),
        "score_std": float(scores.std()),
    }
    return FoldOutcome(number, booster, scores, daily, importance, curve, info)


def _decorate_daily_metrics(
    frame,
    provider,
    universe_sizes,
    label_horizon=DEFAULT_LABEL_HORIZON,
    *,
    snapshot_coverage_end: date | None = None,
):
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["pool_size"] = frame["observation_date"].map(universe_sizes).astype(int)
    mature = mature_label_dates(
        provider.manifest.calendar,
        snapshot_coverage_end or provider.manifest.coverage_end,
        label_horizon,
    )
    for index, row in frame.iterrows():
        reasons = list(row["unavailable_reasons"])
        if row["observation_date"] not in mature and "label_not_mature" not in reasons:
            reasons.append("label_not_mature")
        frame.at[index, "unavailable_reasons"] = reasons
    frame["ic_eligible"] = (
        (frame["factor_coverage"] >= MIN_COVERAGE)
        & (frame["label_coverage"] >= MIN_COVERAGE)
        & (frame["valid_factor_count"] >= MIN_VALID_SECURITIES)
        & (frame["valid_label_count"] >= MIN_VALID_SECURITIES)
    )
    return frame


def _prediction_output(scores, labels, symbols):
    result = scores.rename(
        columns={"observation_date": "datetime", "raw_score": "score"}
    ).copy()
    result["rank"] = result.groupby("datetime")["score"].rank(
        method="min", ascending=False
    ).astype(int)
    label_columns = labels.rename(columns={"observation_date": "datetime"})[
        ["datetime", "instrument_id", "label", "label_reason"]
    ]
    result = result.merge(label_columns, on=["datetime", "instrument_id"], how="left")
    result["label_status"] = result["label_reason"].where(
        result["label_reason"].notna(),
        np.where(result["label"].notna(), "valid", "label_unavailable"),
    )
    result["symbol"] = result["instrument_id"].map(symbols)
    return result[
        ["datetime", "instrument_id", "symbol", "score", "label", "label_status", "rank"]
    ].sort_values(["datetime", "rank"]).reset_index(drop=True)


__all__ = [
    "BuildDayProviderResult",
    "DateRange",
    "DayProviderRef",
    "DirectExperimentConfigError",
    "DirectExperimentDataError",
    "DirectExperimentResult",
    "DirectExperimentTrainingError",
    "DirectPredictionConfig",
    "DirectProviderError",
    "build_day_provider",
    "delete_day_provider",
    "run_direct_prediction",
]
