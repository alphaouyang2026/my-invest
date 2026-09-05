"""Direct DataSnapshot -> Qlib Alpha -> LightGBM experiment.

No task, bundle ORM row, recorder, publication, or artifact is created here.
The three public functions are synchronous and intentionally do not call one
another implicitly.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

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
from app.research.processors import InfToNaN
from app.research.qlib_runtime import read_features
from app.services.calendar_port import CalendarCoverageError, SessionCalendarPort
from app.services.stock_pool import DEFAULT_POLICY, StockPoolError, build_stock_pool

DEFAULT_PROVIDER_ROOT = Path("var/qlib-direct-providers")
FEATURE_SET_ALIASES = {
    "alpha158": "alpha158_jp_v1",
    "alpha360": "alpha360_jp_v1",
}
DEFAULT_LABEL_HORIZON = 5
DEFAULT_ROLLING_STEP = 20


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


@dataclass(frozen=True)
class DirectExperimentResult:
    summary: dict[str, object]
    daily_ic: pd.DataFrame
    predictions: pd.DataFrame
    feature_importance: pd.DataFrame


@dataclass(frozen=True)
class _DirectDataset:
    handler: object
    raw_labels: pd.DataFrame
    feature_rows: int
    label_rows: int


@dataclass(frozen=True)
class _RollingFold:
    number: int
    train: DateRange
    valid: DateRange
    test: DateRange


def run_direct_prediction(
    session: Session,
    config: DirectPredictionConfig,
) -> DirectExperimentResult:
    started = time.monotonic()
    snapshot = _load_and_validate_snapshot(session, config)
    feature_set = _resolve_feature_set(config.feature_set)
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

    universe, universe_sizes, symbols, warnings, policy_fingerprint = _build_universe(
        session, snapshot, calendar, dates, feature_set.max_window
    )
    assembled = _build_dataset(provider, feature_set, config, universe)
    prediction_parts: list[pd.Series] = []
    importance_parts: list[pd.DataFrame] = []
    fold_summaries: list[dict[str, object]] = []
    date_to_fold: dict[date, int] = {}
    for fold in folds:
        dataset = _dataset_for_fold(assembled.handler, fold)
        booster, train_rows, valid_rows, test_features = _train(
            dataset, config.seed, config.num_threads
        )
        try:
            fold_predictions = pd.Series(
                booster.predict(test_features, num_iteration=booster.best_iteration),
                index=test_features.index,
                name="score",
                dtype="float64",
            )
        except Exception as exc:
            raise DirectExperimentTrainingError(
                f"LightGBM prediction failed in fold {fold.number}: {exc}"
            ) from exc
        if fold_predictions.empty or not np.isfinite(fold_predictions.to_numpy()).all():
            raise DirectExperimentTrainingError(
                f"LightGBM produced empty or non-finite predictions in fold {fold.number}"
            )
        constant, reasons = is_constant_model(booster, fold_predictions)
        if constant:
            raise DirectExperimentTrainingError(
                f"Constant LightGBM model in fold {fold.number}: " + "; ".join(reasons)
            )
        prediction_parts.append(fold_predictions)
        fold_importance = feature_importance_frame(booster, list(feature_set.column_names))
        fold_importance.insert(0, "fold", fold.number)
        importance_parts.append(fold_importance)
        for day in dates["fold_test"][fold.number]:
            date_to_fold[day] = fold.number
        fold_summaries.append(
            {
                "fold": fold.number,
                "train": [fold.train.start.isoformat(), fold.train.end.isoformat()],
                "valid": [fold.valid.start.isoformat(), fold.valid.end.isoformat()],
                "test": [fold.test.start.isoformat(), fold.test.end.isoformat()],
                "train_rows": train_rows,
                "valid_rows": valid_rows,
                "test_rows": len(test_features),
                "best_iteration": int(booster.best_iteration),
            }
        )

    raw_predictions = pd.concat(prediction_parts).sort_index()
    if raw_predictions.index.has_duplicates:
        raise DirectExperimentDataError("Rolling folds produced duplicate prediction rows")

    scores = score_frame(raw_predictions)
    daily_ic = evaluate_segment(
        scores,
        assembled.raw_labels,
        universe_sizes=universe_sizes,
        segment="test",
        source="lightgbm",
    )
    daily_ic = _decorate_daily_metrics(
        daily_ic, provider, universe_sizes, config.label_horizon
    )
    daily_ic.insert(0, "fold", daily_ic["observation_date"].map(date_to_fold).astype(int))
    metrics_summary = summarize_segment(daily_ic, segment="test", source="lightgbm")
    if not metrics_summary["observations"]:
        raise DirectExperimentDataError(
            "No test cross-section produced a valid IC after coverage thresholds"
        )

    predictions = _prediction_output(scores, assembled.raw_labels, symbols)
    predictions.insert(0, "fold", predictions["datetime"].map(date_to_fold).astype(int))
    importance = pd.concat(importance_parts, ignore_index=True)
    prediction_dates = int(predictions["datetime"].nunique()) if not predictions.empty else 0
    pool_values = list(universe_sizes.values())
    summary: dict[str, object] = {
        "snapshot_id": str(snapshot.id),
        "snapshot_version": snapshot.version,
        "provider_path": str(provider.path),
        "provider_logical_checksum": provider.manifest.logical_checksum,
        "feature_set": config.feature_set,
        "feature_count": len(feature_set.features),
        "feature_definition_checksum": feature_set.definition_checksum,
        "stock_pool_policy_fingerprint": policy_fingerprint,
        "label_horizon": config.label_horizon,
        "label_expression": (
            f"Ref($close, -{config.label_horizon + 1}) / Ref($close, -1) - 1"
        ),
        "rolling_step": config.rolling_step,
        "rolling_train_policy": "expanding",
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


def _load_and_validate_snapshot(session: Session, config: DirectPredictionConfig) -> DataSnapshot:
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
    try:
        resolve_model_params(None, seed=config.seed)
    except ValueError as exc:
        raise DirectExperimentConfigError(str(exc)) from exc
    return snapshot


def _resolve_feature_set(alias: str):
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

    label_lookahead = config.label_horizon + 1
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
        fold = _RollingFold(
            number=number,
            train=DateRange(config.train.start, all_days[train_end_index]),
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


def _validate_dates(snapshot, calendar, config):
    """Backward-compatible test seam; returns the resolved rolling date sets."""

    return _build_rolling_plan(snapshot, calendar, config)[1]


def _build_universe(session, snapshot, calendar, dates, max_window):
    required = max(max_window, 20)
    policy = replace(
        DEFAULT_POLICY,
        required_history_days=required,
        required_bar_offsets=tuple(sorted({required, 20, 5, 1}, reverse=True)),
    )
    universe: dict[date, set[str]] = {}
    sizes: dict[date, int] = {}
    symbols: dict[str, str] = {}
    warnings: list[dict[str, object]] = []
    seen_warnings: set[str] = set()
    for day in sorted({*dates["train"], *dates["valid"], *dates["test"]}):
        try:
            pool = build_stock_pool(session, snapshot, as_of=day, calendar=calendar, policy=policy)
        except StockPoolError as exc:
            raise DirectExperimentDataError(f"Stock pool failed for {day}: {exc}") from exc
        if not pool.members:
            raise DirectExperimentDataError(f"Stock pool is empty for {day}")
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
    return universe, sizes, symbols, warnings, pool.policy_fingerprint


def _build_dataset(provider, feature_set, config, universe, *, raw_features=None) -> _DirectDataset:
    from qlib.contrib.data.handler import check_transform_proc
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.data.dataset.loader import StaticDataLoader

    fields = list(dict.fromkeys([*feature_set.expressions, "$close"]))
    try:
        raw = raw_features if raw_features is not None else read_features(
            provider.path,
            instruments="all",
            fields=fields,
            start=provider.manifest.coverage_start.isoformat(),
            end=provider.manifest.coverage_end.isoformat(),
        )
    except Exception as exc:
        raise DirectProviderError(f"Qlib feature read failed: {exc}") from exc
    if raw.empty:
        raise DirectExperimentDataError("Qlib returned no feature rows")

    # Filter before copying hundreds of feature columns. The full close series is
    # still retained below: forward labels must include future non-pool dates.
    features = _restrict_to_universe(raw, universe)[list(feature_set.expressions)].copy()
    features.columns = list(feature_set.column_names)
    features = features.swaplevel().sort_index()
    features.index = features.index.set_names(["datetime", "instrument"])

    close = raw["$close"]
    grouped_close = close.groupby(level="instrument")
    forward = (
        grouped_close.shift(-(config.label_horizon + 1))
        / grouped_close.shift(-1)
        - 1
    )
    forward = forward.swaplevel().sort_index()
    forward.index = forward.index.set_names(["datetime", "instrument"])
    forward = forward.reindex(features.index)
    label_frame = forward.rename("LABEL0").to_frame()

    infer_processors = check_transform_proc(
        [InfToNaN(fields_group="feature")], config.train.start, config.train.end
    )
    learn_processors = check_transform_proc(
        [
            {"class": "DropnaLabel"},
            {"class": "CSRankNorm", "kwargs": {"fields_group": "label"}},
        ],
        config.train.start,
        config.train.end,
    )
    handler = DataHandlerLP(
        instruments=None,
        data_loader=StaticDataLoader(config={"feature": features, "label": label_frame}),
        infer_processors=infer_processors,
        learn_processors=learn_processors,
        process_type=DataHandlerLP.PTYPE_A,
    )
    mature_dates = set(provider.manifest.calendar[: -(config.label_horizon + 1)])
    labels = label_frame.reset_index().rename(
        columns={"datetime": "observation_date", "instrument": "instrument_id", "LABEL0": "label"}
    )
    labels["observation_date"] = pd.to_datetime(labels["observation_date"]).dt.date
    labels["instrument_id"] = labels["instrument_id"].astype(str)
    labels["label_reason"] = np.where(
        labels["label"].notna(),
        None,
        np.where(
            labels["observation_date"].isin(mature_dates),
            "label_unavailable",
            "label_not_mature",
        ),
    )
    return _DirectDataset(
        handler=handler,
        raw_labels=labels,
        feature_rows=len(features),
        label_rows=int(label_frame["LABEL0"].notna().sum()),
    )


def _dataset_for_fold(handler, fold: _RollingFold):
    from qlib.data.dataset import DatasetH

    return DatasetH(
        handler=handler,
        segments={
            "train": (fold.train.start, fold.train.end),
            "valid": (fold.valid.start, fold.valid.end),
            "test": (fold.test.start, fold.test.end),
        },
    )


def _restrict_to_universe(frame: pd.DataFrame, universe: dict[date, set[str]]) -> pd.DataFrame:
    dates = frame.index.get_level_values("datetime")
    instruments = frame.index.get_level_values("instrument")
    keep = [
        str(instrument) in universe.get(stamp.date(), ())
        for stamp, instrument in zip(dates, instruments)
    ]
    return frame.loc[keep]


def _train(dataset, seed: int, num_threads: int):
    import lightgbm as lgb
    from qlib.data.dataset.handler import DataHandlerLP

    train = dataset.prepare(
        "train", col_set=["feature", "label"], data_key=DataHandlerLP.DK_L
    )
    valid = dataset.prepare(
        "valid", col_set=["feature", "label"], data_key=DataHandlerLP.DK_L
    )
    test_features = dataset.prepare(
        "test", col_set="feature", data_key=DataHandlerLP.DK_I
    )
    if train.empty:
        raise DirectExperimentDataError("The train segment has no usable rows")
    if valid.empty:
        raise DirectExperimentDataError("The valid segment has no usable rows")
    if test_features.empty:
        raise DirectExperimentDataError("The test segment has no feature rows")

    train_x, train_y = _feature_label_parts(train)
    valid_x, valid_y = _feature_label_parts(valid)
    params = resolve_model_params(None, seed=seed)
    num_boost_round = params.pop("num_boost_round")
    early_stopping_rounds = params.pop("early_stopping_rounds")
    params.update({"metric": "l2", "num_threads": num_threads, "verbosity": -1})
    try:
        train_set = lgb.Dataset(train_x, label=train_y, feature_name=list(train_x.columns))
        valid_set = lgb.Dataset(valid_x, label=valid_y, reference=train_set)
        booster = lgb.train(
            params,
            train_set,
            num_boost_round=num_boost_round,
            valid_sets=[valid_set],
            valid_names=["valid"],
            callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)],
        )
    except Exception as exc:
        raise DirectExperimentTrainingError(f"LightGBM training failed: {exc}") from exc
    return booster, len(train_x), len(valid_x), test_features


def _feature_label_parts(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    if isinstance(frame.columns, pd.MultiIndex):
        features = frame["feature"]
        labels = frame["label"].iloc[:, 0]
    else:
        raise DirectExperimentDataError("Qlib dataset did not preserve feature/label groups")
    return features, labels


def _decorate_daily_metrics(frame, provider, universe_sizes, label_horizon=DEFAULT_LABEL_HORIZON):
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["pool_size"] = frame["observation_date"].map(universe_sizes).astype(int)
    calendar = list(provider.manifest.calendar)
    mature = set(calendar[: -(label_horizon + 1)])
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
