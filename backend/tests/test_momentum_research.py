from datetime import date, timedelta

import pandas as pd
import pytest

from app.research.factor import calculate_momentum_scores


def test_momentum_scores_use_the_declared_endpoints_and_average_ties() -> None:
    days = [date(2025, 1, 6) + timedelta(days=offset) for offset in range(5)]
    closes = pd.DataFrame(
        {
            "alpha": [100.0, 110.0, 120.0, 130.0, 140.0],
            "beta": [100.0, 105.0, 120.0, 126.0, 132.0],
            "gamma": [100.0, 110.0, 120.0, 130.0, 140.0],
        },
        index=days,
    )

    scores = calculate_momentum_scores(
        closes,
        observation_dates=[days[-1]],
        lookback_days=2,
        skip_days=1,
    ).set_index("instrument_id")

    assert scores.loc["alpha", "raw_score"] == pytest.approx(0.18181818)
    assert scores.loc["beta", "raw_score"] == pytest.approx(0.2)
    assert scores.loc["gamma", "raw_score"] == pytest.approx(0.18181818)
    assert scores.loc["alpha", "average_rank"] == 1.5
    assert scores.loc["gamma", "average_rank"] == 1.5
    assert scores.loc["beta", "average_rank"] == 3.0
    assert scores.loc["alpha", "rank_percentile"] == pytest.approx(0.5)
    assert scores.loc["beta", "rank_percentile"] == pytest.approx(1.0)


def test_internal_price_gaps_below_ninety_percent_keep_the_member_but_exclude_the_score() -> None:
    days = [date(2025, 1, 6) + timedelta(days=offset) for offset in range(6)]
    closes = pd.DataFrame(
        {"thin_history": [100.0, 101.0, float("nan"), 103.0, 104.0, 105.0]},
        index=days,
    )

    row = calculate_momentum_scores(
        closes,
        observation_dates=[days[-1]],
        lookback_days=3,
        skip_days=1,
    ).iloc[0]

    assert row["path_coverage"] == pytest.approx(0.75)
    assert pd.isna(row["raw_score"])
    assert row["factor_reason"] == "insufficient_path_coverage"
