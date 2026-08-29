"""Scoring a model, and scoring 6-1 momentum beside it on the same rows.

Every number here comes out of ticket 06's `evaluate_cross_section` and
`summarize_ic`. That is not laziness about reuse — it is the precondition for
the comparison meaning anything. The two curves this module produces differ in
exactly one respect, how the score was arrived at; they share the universe, the
labels, the coverage thresholds and the statistics code. One divergence
anywhere in that list and "the model beats momentum" stops being a statement
about the model.

All three segments are evaluated, but only `test` is a conclusion. Train Rank IC
of 0.15 against test Rank IC of 0.01 is the most direct evidence of overfitting
there is, and showing only the test column hides it.

The scores fed in are the model's raw regression output. It has no return unit
and no percentile unit — the training target was a rank transform, and the
regression value is not calibrated back to anything. Only `rank_percentile`,
obtained by re-ranking within a cross-section, is a position.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

import pandas as pd

from app.core.logging import get_logger
from app.research.evaluation import (
    evaluate_cross_section,
    flatten_group_returns,
    summarize_ic,
)
from app.research.model_definition import GROUP_COUNT, MIN_COVERAGE, MIN_VALID_SECURITIES

logger = get_logger(__name__)


def score_frame(predictions: pd.Series) -> pd.DataFrame:
    """Qlib's `(datetime, instrument) -> score` into the shape 06 evaluates.

    Ranking happens per cross-section and nowhere else. A rank computed over the
    whole panel would compare a security in March against one in January, which
    is not a question anyone asked.
    """
    if predictions.empty:
        return pd.DataFrame(
            columns=[
                "observation_date",
                "instrument_id",
                "raw_score",
                "average_rank",
                "rank_percentile",
                "normalized_score",
            ]
        )
    frame = predictions.rename("raw_score").reset_index()
    frame = frame.rename(columns={"datetime": "observation_date", "instrument": "instrument_id"})
    frame["observation_date"] = pd.to_datetime(frame["observation_date"]).dt.date
    frame["instrument_id"] = frame["instrument_id"].astype(str)

    grouped = frame.groupby("observation_date")["raw_score"]
    frame["average_rank"] = grouped.rank(method="average")
    # Denominator is the cross-section's own size, so the top name is 1.0
    # whatever the pool size was that week.
    frame["rank_percentile"] = grouped.rank(method="average", pct=True)
    # A display column only. Kept separate from `rank_percentile` because the
    # interface downstream consumes the percentile, and a z-score is not one.
    frame["normalized_score"] = grouped.transform(
        lambda values: (values - values.mean()) / values.std(ddof=0)
        if values.std(ddof=0)
        else 0.0
    )
    return frame


def evaluate_segment(
    scores: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    universe_sizes: dict[date, int],
    segment: str,
    source: str,
) -> pd.DataFrame:
    """Per-cross-section metrics for one segment and one score source."""
    rows: list[dict] = []
    if scores.empty:
        return pd.DataFrame(rows)
    for observation_date in sorted(set(scores["observation_date"])):
        universe_size = universe_sizes.get(observation_date)
        if not universe_size:
            continue
        result = evaluate_cross_section(
            scores[scores["observation_date"] == observation_date],
            labels[labels["observation_date"] == observation_date],
            universe_size=universe_size,
            min_coverage=MIN_COVERAGE,
            min_valid_securities=MIN_VALID_SECURITIES,
            group_count=GROUP_COUNT,
        )
        row = asdict(result)
        row.update(flatten_group_returns(row.pop("group_returns")))
        row["unavailable_reasons"] = list(result.unavailable_reasons)
        row.update(
            {"observation_date": observation_date, "segment": segment, "source": source}
        )
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_segment(metrics: pd.DataFrame, *, segment: str, source: str) -> dict:
    """Scalars for one segment, including how many cross-sections produced them.

    `observations` travels with every statistic on purpose. A Rank ICIR computed
    from sixteen weekly observations is not the same claim as one computed from
    eight hundred, and separating the number from its sample size is how the
    former gets read as the latter.
    """
    if metrics.empty:
        return {
            "segment": segment,
            "source": source,
            "observations": 0,
            "ic_mean": None,
            "icir": None,
            "rank_ic_mean": None,
            "rank_icir": None,
            "long_short_mean": None,
            "group_monotonicity": None,
        }
    ic = summarize_ic(list(metrics["ic"]))
    rank_ic = summarize_ic(list(metrics["rank_ic"]))
    long_short = pd.Series(metrics["long_short_return"], dtype="float64").dropna()
    return {
        "segment": segment,
        "source": source,
        "observations": int(ic.count),
        "ic_mean": ic.mean,
        "icir": ic.icir,
        "ic_unavailable_reason": ic.unavailable_reason,
        "rank_ic_mean": rank_ic.mean,
        "rank_icir": rank_ic.icir,
        "rank_ic_unavailable_reason": rank_ic.unavailable_reason,
        "long_short_mean": float(long_short.mean()) if not long_short.empty else None,
        "group_monotonicity": group_monotonicity(metrics),
    }


def group_monotonicity(metrics: pd.DataFrame) -> float | None:
    """Spearman between quintile number and quintile return.

    A more legible answer to "did the model do anything" than mean IC, and
    steadier: one freak cross-section can dominate an IC average, while the
    ordering of five bucket means is harder to knock over.
    """
    columns = [f"group_return_{group}" for group in range(1, GROUP_COUNT + 1)]
    if not set(columns) <= set(metrics.columns):
        return None
    means = metrics[columns].mean()
    if means.isna().any() or means.nunique() < 2:
        # Undefined, not zero. "Could not be computed" and "computed and found
        # no ordering" are different findings (06 section 9.1).
        return None
    return float(
        pd.Series(range(1, GROUP_COUNT + 1), dtype="float64").corr(
            pd.Series(means.to_numpy(), dtype="float64"), method="spearman"
        )
    )


def feature_importance_frame(booster, feature_names: list[str]) -> pd.DataFrame:
    """Both LightGBM importance measures, ranked.

    `split` counts how often a feature was chosen and systematically flatters
    high-cardinality continuous columns, which are simply easier to keep
    splitting on. `gain` is closer to "how much did it contribute". Publishing
    both is what exposes a feature used hundreds of times that contributes
    almost nothing.
    """
    gain = booster.feature_importance("gain")
    split = booster.feature_importance("split")
    frame = pd.DataFrame(
        {
            "feature_name": feature_names,
            "gain": [float(value) for value in gain],
            "split": [int(value) for value in split],
        }
    )
    frame["gain_rank"] = frame["gain"].rank(method="min", ascending=False).astype(int)
    frame["split_rank"] = frame["split"].rank(method="min", ascending=False).astype(int)
    return frame.sort_values("gain_rank").reset_index(drop=True)


def training_curve_frame(evals_result: dict) -> pd.DataFrame:
    """Validation loss per boosting round, for the overfitting picture."""
    rows: list[dict] = []
    for dataset_name, metrics in (evals_result or {}).items():
        for metric_name, values in metrics.items():
            rows.extend(
                {
                    "dataset": dataset_name,
                    "metric": metric_name,
                    "iteration": iteration,
                    "value": float(value),
                }
                for iteration, value in enumerate(values)
            )
    return pd.DataFrame(rows)
