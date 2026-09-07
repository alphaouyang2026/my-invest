"""The one place LightGBM is trained for either experiment entry point.

Both entry points used to own a training loop. They agreed on the objective and
disagreed on everything the objective is selected against: the search evaluated
a custom `rank_ic`/`l2` pair through `feval`, the direct run took LightGBM's
built-in `l2` on a single validation set. A parameter set that won the search
therefore had no defensible meaning on the direct run, which is the entry point
that actually produces predictions.

The two are numerically the same on `l2` -- same selected round, bit-identical
predictions, and a `model.txt` differing only in the recorded metric name -- so
unifying costs the direct run nothing and buys the search a transferable answer.
`stop_metric` is what the two genuinely disagreed about, so it is an argument
rather than a constant.

No test data reaches this seam. The caller hands over two bounded segments and
nothing else can select the round.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

STOP_METRICS = ("l2", "rank_ic")
MIN_RANK_IC_SECURITIES = 100


class BoosterTrainingError(ValueError):
    """A segment this seam refuses to train on."""


@dataclass(frozen=True)
class TrainingMatrix:
    """One bounded segment, already stripped of its index semantics.

    `dates` is per-row rather than an index level so a caller holding a NumPy
    matrix does not have to rebuild a MultiIndex just to be trained on.
    """

    features: Any
    labels: np.ndarray
    dates: Any
    feature_names: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.labels)


@dataclass(frozen=True)
class TrainingOutcome:
    booster: Any
    curve: pd.DataFrame
    info: dict[str, float | int]


def rank_evaluator(labels, dates, min_count=MIN_RANK_IC_SECURITIES):
    """Precompute label ranks; avoid rescanning the entire panel once per date per round."""
    codes, _ = pd.factorize(dates, sort=True)
    counts = np.bincount(codes)
    yr = pd.Series(labels).groupby(codes).rank(method="average").to_numpy()
    yc = yr - (np.bincount(codes, weights=yr)/counts)[codes]
    yy = np.bincount(codes, weights=yc*yc)

    def evaluate(pred):
        if not np.isfinite(pred).all():
            raise ValueError("Nonfinite training predictions")
        ranks = pd.Series(pred).groupby(codes).rank(method="average").to_numpy()
        centered = ranks - (np.bincount(codes, weights=ranks)/counts)[codes]
        xx = np.bincount(codes, weights=centered*centered)
        xy = np.bincount(codes, weights=centered*yc)
        valid = (counts >= min_count) & (xx > 0) & (yy > 0)
        if not valid.any():
            raise ValueError("No valid daily Rank IC")
        return float(np.mean(xy[valid]/np.sqrt(xx[valid]*yy[valid])))
    return evaluate


def train_booster(
    train: TrainingMatrix,
    valid: TrainingMatrix,
    params: dict[str, Any],
    *,
    stop_metric: Literal["l2", "rank_ic"],
    num_threads: int,
) -> TrainingOutcome:
    """Fit one booster and report what selected its round.

    `params` arrives already resolved -- this seam does not merge defaults or
    consult a whitelist, so the caller cannot be misled about what ran.
    """
    import lightgbm as lgb

    if stop_metric not in STOP_METRICS:
        raise BoosterTrainingError(f"stop_metric must be one of {list(STOP_METRICS)}, got {stop_metric!r}")
    train_labels = np.asarray(train.labels)
    valid_labels = np.asarray(valid.labels)
    if (
        not len(train)
        or not len(valid)
        or not np.isfinite(train_labels).all()
        or not np.isfinite(valid_labels).all()
    ):
        raise BoosterTrainingError("Empty segment or nonfinite learning labels")

    params = dict(params)
    rounds = params.pop("num_boost_round")
    patience = params.pop("early_stopping_rounds")
    params.update(metric="None", verbosity=-1, num_threads=num_threads)

    feature_names = list(train.feature_names)
    train_set = lgb.Dataset(train.features, label=train_labels, feature_name=feature_names)
    valid_set = lgb.Dataset(valid.features, label=valid_labels, reference=train_set)
    evaluators = {
        id(train_set): rank_evaluator(train_labels, train.dates),
        id(valid_set): rank_evaluator(valid_labels, valid.dates),
    }

    def evaluate(predictions, data):
        labels = data.get_label()
        rank = evaluators[id(data)](predictions)
        metrics = [
            ("rank_ic", rank, True),
            ("l2", float(np.mean((predictions - labels) ** 2)), False),
        ]
        return metrics if stop_metric == "rank_ic" else metrics[::-1]

    history: dict = {}
    booster = lgb.train(
        params,
        train_set,
        num_boost_round=rounds,
        valid_sets=[train_set, valid_set],
        valid_names=["train", "valid"],
        feval=evaluate,
        callbacks=[
            lgb.record_evaluation(history),
            lgb.early_stopping(patience, first_metric_only=True, verbose=False),
        ],
    )
    curve = pd.DataFrame(
        [
            dict(dataset=dataset, metric=metric, iteration=index + 1, value=float(value))
            for dataset, metrics in history.items()
            for metric, values in metrics.items()
            for index, value in enumerate(values)
        ]
    )
    info = dict(
        best_iteration=int(booster.best_iteration),
        evaluated_rounds=len(history["valid"]["l2"]),
        constant_baseline_l2=float(np.mean((valid_labels - float(train_labels.mean())) ** 2)),
        best_valid_l2=float(history["valid"]["l2"][booster.best_iteration - 1]),
        best_valid_rank_ic=float(history["valid"]["rank_ic"][booster.best_iteration - 1]),
    )
    return TrainingOutcome(booster, curve, info)
