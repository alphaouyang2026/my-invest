from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class CrossSectionEvaluation:
    factor_coverage: float
    label_coverage: float
    valid_factor_count: int
    valid_label_count: int
    paired_count: int
    ic: float | None
    rank_ic: float | None
    group_returns: dict[int, float] = field(default_factory=dict)
    long_short_return: float | None = None
    unavailable_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class IcSummary:
    count: int
    mean: float | None
    icir: float | None
    unavailable_reason: str | None = None


def summarize_ic(values: list[float | None]) -> IcSummary:
    valid = pd.Series(values, dtype="float64").dropna()
    summary = _summarize_ic(valid)
    logger.debug(
        "evaluation.ic_summarized",
        offered=len(values),
        used=summary.count,
        mean=summary.mean,
        icir=summary.icir,
        unavailable_reason=summary.unavailable_reason,
    )
    return summary


def _summarize_ic(valid: pd.Series) -> IcSummary:
    if valid.empty:
        return IcSummary(0, None, None, "no_valid_observations")
    mean = float(valid.mean())
    if len(valid) < 2:
        return IcSummary(len(valid), mean, None, "insufficient_observations")
    sample_std = float(valid.std(ddof=1))
    if sample_std == 0:
        return IcSummary(len(valid), mean, None, "zero_variance")
    return IcSummary(len(valid), mean, mean / sample_std)


def flatten_group_returns(group_returns: dict[int, float]) -> dict[str, float]:
    """Quintile returns as columns of their own, for a tabular artifact.

    The mapping is keyed by group number because that is what it means, and
    `long_short_return` subtracts one group from another. Parquet has nowhere
    to put that: Arrow struct fields and map keys are strings. Fixed column
    names also keep the table readable without unpacking a nested value on
    every row.
    """
    return {f"group_return_{group}": value for group, value in sorted(group_returns.items())}


def evaluate_cross_section(
    scores: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    universe_size: int,
    min_coverage: float = 0.90,
    min_valid_securities: int = 100,
    group_count: int = 5,
) -> CrossSectionEvaluation:
    """Evaluate one frozen universe without changing its denominator."""
    if universe_size < 1:
        raise ValueError("universe_size must be positive")
    valid_scores = scores[scores["raw_score"].notna()].copy()
    valid_labels = labels[labels["label"].notna()].copy()
    factor_coverage = len(valid_scores) / universe_size
    label_coverage = len(valid_labels) / universe_size
    joined = valid_scores.merge(
        valid_labels[["observation_date", "instrument_id", "label"]],
        on=["observation_date", "instrument_id"],
        how="inner",
    )

    reasons: list[str] = []
    if factor_coverage < min_coverage or len(valid_scores) < min_valid_securities:
        reasons.append("insufficient_factor_coverage")
    if label_coverage < min_coverage or len(valid_labels) < min_valid_securities:
        reasons.append("insufficient_label_coverage")
    if reasons:
        return CrossSectionEvaluation(
            factor_coverage=factor_coverage,
            label_coverage=label_coverage,
            valid_factor_count=len(valid_scores),
            valid_label_count=len(valid_labels),
            paired_count=len(joined),
            ic=None,
            rank_ic=None,
            unavailable_reasons=tuple(reasons),
        )

    if joined["raw_score"].nunique(dropna=True) < 2 or joined["label"].nunique(dropna=True) < 2:
        reasons.append("zero_variance")
        ic = None
        rank_ic = None
    else:
        ic = float(joined["raw_score"].corr(joined["label"], method="pearson"))
        label_rank = joined["label"].rank(method="average")
        rank_ic = float(joined["average_rank"].corr(label_rank, method="pearson"))

    groups = joined.assign(
        group=joined["rank_percentile"].map(
            lambda value: min(group_count, max(1, math.ceil(float(value) * group_count)))
        )
    ).groupby("group")["label"].mean()
    group_returns = {int(group): float(value) for group, value in groups.items()}
    long_short = (
        group_returns[group_count] - group_returns[1]
        if 1 in group_returns and group_count in group_returns
        else None
    )
    if long_short is None:
        reasons.append("missing_extreme_group")

    evaluation = CrossSectionEvaluation(
        factor_coverage=factor_coverage,
        label_coverage=label_coverage,
        valid_factor_count=len(valid_scores),
        valid_label_count=len(valid_labels),
        paired_count=len(joined),
        ic=ic,
        rank_ic=rank_ic,
        group_returns=group_returns,
        long_short_return=long_short,
        unavailable_reasons=tuple(reasons),
    )
    logger.debug(
        "evaluation.cross_section",
        universe_size=universe_size,
        factor_coverage=round(factor_coverage, 4),
        label_coverage=round(label_coverage, 4),
        paired=len(joined),
        ic=ic,
        rank_ic=rank_ic,
        unavailable_reasons=list(reasons),
    )
    return evaluation
