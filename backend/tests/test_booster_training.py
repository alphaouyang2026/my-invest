"""The single LightGBM training call both experiment entry points make.

Not to be confused with `test_lgbm_seam.py`, which covers the product path's
`app/research/lgbm.py`. This one covers `app/experiments/booster_training.py`:
the function a search ranks candidates with and a run later executes them with,
which are the same function precisely so that a ranking transfers.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.experiments.booster_training import (
    BoosterTrainingError,
    TrainingMatrix,
    rank_evaluator,
    train_booster,
)
from app.research.model_definition import resolve_model_params

PARAMS = resolve_model_params(
    {"num_boost_round": 12, "early_stopping_rounds": 3, "min_data_in_leaf": 20},
    seed=20260829,
)


def test_rank_metric_is_equal_weight_per_day():
    """A day with 200 securities must not outweigh a day with 100."""
    y = np.r_[np.arange(100), np.arange(200)]
    p = np.r_[np.arange(100), -np.arange(200)]
    dates = np.r_[np.zeros(100), np.ones(200)]
    assert rank_evaluator(y, dates, min_count=50)(p) == pytest.approx(0)


def synthetic_segments(cal, columns=6):
    rng = np.random.default_rng(7)
    index = pd.MultiIndex.from_product(
        [pd.to_datetime(cal), [str(i) for i in range(120)]], names=["datetime", "instrument"]
    )
    features = rng.normal(size=(len(index), columns)).astype("float32")
    labels = features[:, 0] * 0.8 + rng.normal(scale=0.2, size=len(index))
    names = tuple(f"f{i}" for i in range(columns))
    return TrainingMatrix(features, labels, index.get_level_values("datetime"), names)


@pytest.mark.parametrize("metric", ["l2","rank_ic"])
def test_real_lightgbm_stops_only_on_selected_validation_metric(metric):
    """The shared seam still honours whichever metric a trial selected."""
    cal = pd.bdate_range("2020-01-01",periods=8).strftime("%Y-%m-%d").tolist()
    train = synthetic_segments(cal[:5])
    valid = synthetic_segments(cal[5:])
    outcome = train_booster(
        train, valid,
        PARAMS, stop_metric=metric, num_threads=2,
    )
    curve = outcome.curve
    values = curve[(curve.dataset == "valid") & (curve.metric == metric)]
    expected = values.loc[
        values.value.idxmin() if metric == "l2" else values.value.idxmax(), "iteration"
    ]
    assert outcome.booster.best_iteration == expected == outcome.info["best_iteration"]
    assert set(curve.dataset) == {"train", "valid"}
    assert set(curve.metric) == {"l2", "rank_ic"}




def test_an_empty_or_nonfinite_segment_is_refused() -> None:
    """A segment that cannot teach anything must say so rather than train."""
    cal = pd.bdate_range("2020-01-01", periods=4).strftime("%Y-%m-%d").tolist()
    good = synthetic_segments(cal)
    empty = TrainingMatrix(
        good.features[:0], good.labels[:0], good.dates[:0], good.feature_names
    )
    with pytest.raises(BoosterTrainingError, match="Empty segment"):
        train_booster(empty, good, PARAMS,
                      stop_metric="l2", num_threads=1)

    nonfinite = TrainingMatrix(
        good.features, np.where(np.arange(len(good.labels)) == 0, np.nan, good.labels),
        good.dates, good.feature_names,
    )
    with pytest.raises(BoosterTrainingError, match="nonfinite learning labels"):
        train_booster(nonfinite, good, PARAMS,
                      stop_metric="l2", num_threads=1)


def test_an_unknown_stop_metric_is_refused() -> None:
    cal = pd.bdate_range("2020-01-01", periods=4).strftime("%Y-%m-%d").tolist()
    good = synthetic_segments(cal)
    with pytest.raises(BoosterTrainingError, match="stop_metric must be"):
        train_booster(good, good, PARAMS,
                      stop_metric="sharpe", num_threads=1)
