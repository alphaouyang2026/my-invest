from datetime import date, timedelta

import pandas as pd
import pytest

from app.research.labels import calculate_daily_labels, calculate_weekly_labels


def test_daily_label_is_next_open_to_sixth_following_open() -> None:
    days = [date(2025, 1, 6) + timedelta(days=offset) for offset in range(8)]
    opens = pd.DataFrame(
        {"alpha": [90.0, 100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0]},
        index=days,
    )

    row = calculate_daily_labels(opens, observation_dates=[days[0]]).iloc[0]

    assert row["entry_date"] == days[1]
    assert row["exit_date"] == days[6]
    assert row["label"] == pytest.approx(0.5)
    assert row["label_reason"] is None


def test_weekly_label_uses_the_next_actual_rebalance_opens() -> None:
    days = [
        date(2025, 1, 9),
        date(2025, 1, 10),
        date(2025, 1, 14),  # Monday was a market holiday.
        date(2025, 1, 15),
        date(2025, 1, 16),
        date(2025, 1, 17),
        date(2025, 1, 20),
    ]
    opens = pd.DataFrame(
        {"alpha": [90.0, 95.0, 100.0, 110.0, 120.0, 125.0, 130.0]},
        index=days,
    )

    rows = calculate_weekly_labels(
        opens,
        weekly_observation_dates=[date(2025, 1, 10), date(2025, 1, 17)],
    )
    first = rows[rows["observation_date"] == date(2025, 1, 10)].iloc[0]

    assert first["entry_date"] == date(2025, 1, 14)
    assert first["exit_date"] == date(2025, 1, 20)
    assert first["label"] == pytest.approx(0.3)
