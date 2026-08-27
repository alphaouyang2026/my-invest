from datetime import date

import pandas as pd
import pytest

from app.research.evaluation import evaluate_cross_section, summarize_ic


def test_cross_section_reports_ic_rank_ic_groups_and_long_short() -> None:
    observation = date(2025, 1, 31)
    scores = pd.DataFrame(
        {
            "observation_date": [observation] * 5,
            "instrument_id": ["a", "b", "c", "d", "e"],
            "raw_score": [1.0, 2.0, 3.0, 4.0, 5.0],
            "average_rank": [1.0, 2.0, 3.0, 4.0, 5.0],
            "rank_percentile": [0.2, 0.4, 0.6, 0.8, 1.0],
        }
    )
    labels = pd.DataFrame(
        {
            "observation_date": [observation] * 5,
            "instrument_id": ["a", "b", "c", "d", "e"],
            "label": [0.01, 0.02, 0.03, 0.04, 0.05],
        }
    )

    result = evaluate_cross_section(
        scores,
        labels,
        universe_size=5,
        min_valid_securities=2,
    )

    assert result.factor_coverage == 1.0
    assert result.label_coverage == 1.0
    assert result.ic == pytest.approx(1.0)
    assert result.rank_ic == pytest.approx(1.0)
    assert result.group_returns == pytest.approx({1: 0.01, 2: 0.02, 3: 0.03, 4: 0.04, 5: 0.05})
    assert result.long_short_return == pytest.approx(0.04)


def test_icir_is_mean_over_sample_standard_deviation_without_annualization() -> None:
    summary = summarize_ic([1.0, 2.0, 3.0])

    assert summary.mean == pytest.approx(2.0)
    assert summary.icir == pytest.approx(2.0)
    assert summary.unavailable_reason is None
