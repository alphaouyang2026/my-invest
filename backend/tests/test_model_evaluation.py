"""Model metrics, and the momentum curve computed beside them.

The comparison in the design is only meaningful because both curves go through
the same statistics code on the same rows. So the tests here care less about
individual numbers than about that shared path staying shared, and about
"undefined" never quietly becoming zero.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.research.model_evaluation import (
    evaluate_segment,
    feature_importance_frame,
    group_monotonicity,
    prediction_frame,
    score_frame,
    summarize_segment,
    training_curve_frame,
)

WEEKS = [date(2025, 3, 7) + timedelta(days=7 * offset) for offset in range(6)]
SECURITIES = [f"s{index:04d}" for index in range(120)]


def _predictions(seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    index = pd.MultiIndex.from_product(
        [pd.to_datetime(WEEKS), SECURITIES], names=["datetime", "instrument"]
    )
    return pd.Series(rng.normal(size=len(index)), index=index)


def _labels(scores: pd.DataFrame, *, informative: bool) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    noise = rng.normal(scale=0.02, size=len(scores))
    signal = scores["raw_score"].to_numpy() * 0.01 if informative else 0.0
    return pd.DataFrame(
        {
            "observation_date": scores["observation_date"],
            "instrument_id": scores["instrument_id"],
            "label": signal + noise,
        }
    )


def _universe_sizes() -> dict[date, int]:
    return {week: len(SECURITIES) for week in WEEKS}


def test_ranking_happens_inside_each_cross_section() -> None:
    """A rank across the whole panel would compare March against January."""
    frame = score_frame(_predictions())

    for _, cross_section in frame.groupby("observation_date"):
        assert cross_section["average_rank"].min() == 1
        assert cross_section["average_rank"].max() == len(SECURITIES)
        assert cross_section["rank_percentile"].max() == pytest.approx(1.0)


def test_the_raw_score_is_left_untouched_by_ranking() -> None:
    """`raw_score` has no percentile meaning; only `rank_percentile` does."""
    predictions = _predictions()
    frame = score_frame(predictions).set_index(["observation_date", "instrument_id"])

    sample = frame.iloc[0]
    assert sample["raw_score"] != sample["rank_percentile"]
    assert not frame["raw_score"].between(0, 1).all()


def test_published_predictions_carry_label_state_and_provenance() -> None:
    scores = score_frame(_predictions()).iloc[:2].copy()
    labels = pd.DataFrame(
        [
            {
                "observation_date": scores.iloc[0]["observation_date"],
                "instrument_id": scores.iloc[0]["instrument_id"],
                "label": 0.01,
                "label_reason": None,
            },
            {
                "observation_date": scores.iloc[1]["observation_date"],
                "instrument_id": scores.iloc[1]["instrument_id"],
                "label": None,
                "label_reason": "label_not_matured",
            },
        ]
    )

    published = prediction_frame(
        scores,
        labels,
        trained_model_id="model-1",
        data_snapshot_id="snapshot-1",
    )

    assert list(published["label_status"]) == ["valid", "label_not_matured"]
    assert set(published["trained_model_id"]) == {"model-1"}
    assert set(published["data_snapshot_id"]) == {"snapshot-1"}
    # The security code is not in the artifact: it is resolved from
    # `instruments.source_code` when the scores are read, so the immutable file
    # carries the identity and not a display label.
    assert "symbol" not in published
    assert "instrument_id" in published


def test_an_informative_score_and_a_noise_score_go_through_one_code_path() -> None:
    """The property the model/momentum comparison rests on.

    Both curves are produced by the same call with the same universe, labels and
    thresholds — the only difference is where the scores came from.
    """
    model = score_frame(_predictions(seed=0))
    momentum = score_frame(_predictions(seed=99))
    labels = _labels(model, informative=True)

    model_metrics = evaluate_segment(
        model, labels, universe_sizes=_universe_sizes(), segment="test", source="model"
    )
    momentum_metrics = evaluate_segment(
        momentum, labels, universe_sizes=_universe_sizes(), segment="test", source="momentum"
    )

    assert list(model_metrics.columns) == list(momentum_metrics.columns)
    assert set(model_metrics["observation_date"]) == set(momentum_metrics["observation_date"])
    assert summarize_segment(model_metrics, segment="test", source="model")["rank_ic_mean"] > (
        summarize_segment(momentum_metrics, segment="test", source="momentum")["rank_ic_mean"]
    )


def test_every_summary_carries_its_observation_count() -> None:
    """Sixteen weekly observations is not the same claim as eight hundred."""
    scores = score_frame(_predictions())
    metrics = evaluate_segment(
        scores,
        _labels(scores, informative=True),
        universe_sizes=_universe_sizes(),
        segment="test",
        source="model",
    )

    summary = summarize_segment(metrics, segment="test", source="model")

    assert summary["observations"] == len(WEEKS)


def test_a_cross_section_below_the_universe_threshold_is_excluded_not_scored() -> None:
    scores = score_frame(_predictions())
    labels = _labels(scores, informative=True)
    # Ten times the securities actually present, so coverage falls under 90%.
    sizes = {week: len(SECURITIES) * 10 for week in WEEKS}

    metrics = evaluate_segment(
        scores, labels, universe_sizes=sizes, segment="test", source="model"
    )

    assert metrics["ic"].isna().all()
    assert all("insufficient_factor_coverage" in row for row in metrics["unavailable_reasons"])


def test_an_empty_segment_summarises_to_unavailable_rather_than_zero() -> None:
    summary = summarize_segment(pd.DataFrame(), segment="valid", source="model")

    assert summary["observations"] == 0
    assert summary["ic_mean"] is None
    assert summary["rank_icir"] is None
    assert summary["group_monotonicity"] is None


def test_group_monotonicity_is_one_when_quintiles_are_ordered() -> None:
    metrics = pd.DataFrame(
        [{f"group_return_{group}": group * 0.001 for group in range(1, 6)}]
    )

    assert group_monotonicity(metrics) == pytest.approx(1.0)


def test_group_monotonicity_is_unavailable_when_it_cannot_be_computed() -> None:
    """Undefined is not zero: "could not compute" and "found no ordering" are
    different findings, and collapsing them loses the one that matters."""
    flat = pd.DataFrame([{f"group_return_{group}": 0.001 for group in range(1, 6)}])

    assert group_monotonicity(flat) is None
    assert group_monotonicity(pd.DataFrame([{"ic": 0.1}])) is None


def test_feature_importance_reports_both_measures() -> None:
    """`split` flatters high-cardinality columns; `gain` is closer to
    contribution. Both are published so a feature used often and contributing
    nothing is visible."""
    import lightgbm as lgb

    rng = np.random.default_rng(3)
    x = rng.normal(size=(4000, 4))
    y = x[:, 0] * 2 + rng.normal(scale=0.2, size=4000)
    booster = lgb.train(
        {"objective": "mse", "verbosity": -1, "num_leaves": 15, "seed": 1},
        lgb.Dataset(x, label=y),
        num_boost_round=20,
    )

    frame = feature_importance_frame(booster, ["A", "B", "C", "D"])

    assert list(frame.columns) == ["feature_name", "gain", "split", "gain_rank", "split_rank"]
    assert frame.iloc[0]["feature_name"] == "A"
    assert frame.iloc[0]["gain_rank"] == 1


def test_the_training_curve_is_flattened_per_round() -> None:
    curve = training_curve_frame({"valid": {"l2": [0.9, 0.7, 0.6]}})

    assert len(curve) == 3
    assert list(curve["iteration"]) == [0, 1, 2]
    assert curve.iloc[-1]["value"] == pytest.approx(0.6)
