from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pandas as pd

from app.core.logging import get_logger

logger = get_logger(__name__)


def calculate_daily_labels(
    opens: pd.DataFrame,
    *,
    observation_dates: Iterable[date],
) -> pd.DataFrame:
    """Five-session forward return: next open to sixth following open."""
    rows: list[dict] = []
    for observation_date in observation_dates:
        try:
            position = opens.index.get_loc(observation_date)
        except KeyError as exc:
            raise ValueError(f"{observation_date} is not in the trading calendar") from exc
        if not isinstance(position, int):
            raise ValueError("trading calendar index must be unique")

        matured = position + 6 < len(opens.index)
        entry_date = opens.index[position + 1] if position + 1 < len(opens.index) else None
        exit_date = opens.index[position + 6] if matured else None
        for instrument_id in opens.columns:
            if not matured:
                label = None
                reason = "label_not_matured"
            else:
                entry = opens.loc[entry_date, instrument_id]
                exit_value = opens.loc[exit_date, instrument_id]
                if pd.isna(entry) or entry <= 0:
                    label = None
                    reason = "missing_entry_open"
                elif pd.isna(exit_value) or exit_value <= 0:
                    label = None
                    reason = "missing_exit_open"
                else:
                    label = float(exit_value / entry - 1.0)
                    reason = None
            rows.append(
                {
                    "observation_date": observation_date,
                    "instrument_id": str(instrument_id),
                    "label_frequency": "daily",
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "label": label,
                    "label_reason": reason,
                }
            )
    logger.debug("labels.daily_calculated", rows=len(rows))
    return pd.DataFrame.from_records(rows)


def calculate_weekly_labels(
    opens: pd.DataFrame,
    *,
    weekly_observation_dates: Iterable[date],
) -> pd.DataFrame:
    """Return one weekly holding-period label per declared rebalance cross-section."""
    observations = list(weekly_observation_dates)
    rows: list[dict] = []
    for index, observation_date in enumerate(observations):
        try:
            position = opens.index.get_loc(observation_date)
        except KeyError as exc:
            raise ValueError(f"{observation_date} is not in the trading calendar") from exc
        if not isinstance(position, int):
            raise ValueError("trading calendar index must be unique")

        next_observation = observations[index + 1] if index + 1 < len(observations) else None
        next_position = opens.index.get_loc(next_observation) if next_observation else None
        matured = (
            isinstance(next_position, int)
            and position + 1 < len(opens.index)
            and next_position + 1 < len(opens.index)
        )
        entry_date = opens.index[position + 1] if position + 1 < len(opens.index) else None
        exit_date = opens.index[next_position + 1] if matured else None

        for instrument_id in opens.columns:
            if not matured:
                label = None
                reason = "label_not_matured"
            else:
                entry = opens.loc[entry_date, instrument_id]
                exit_value = opens.loc[exit_date, instrument_id]
                if pd.isna(entry) or entry <= 0:
                    label = None
                    reason = "missing_entry_open"
                elif pd.isna(exit_value) or exit_value <= 0:
                    label = None
                    reason = "missing_exit_open"
                else:
                    label = float(exit_value / entry - 1.0)
                    reason = None
            rows.append(
                {
                    "observation_date": observation_date,
                    "instrument_id": str(instrument_id),
                    "label_frequency": "weekly",
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "label": label,
                    "label_reason": reason,
                }
            )
    logger.debug("labels.weekly_calculated", rows=len(rows))
    return pd.DataFrame.from_records(rows)
