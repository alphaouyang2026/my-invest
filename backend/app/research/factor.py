from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pandas as pd

from app.core.logging import get_logger

logger = get_logger(__name__)


def calculate_momentum_scores(
    closes: pd.DataFrame,
    *,
    observation_dates: Iterable[date],
    lookback_days: int,
    skip_days: int,
) -> pd.DataFrame:
    """Calculate registered momentum scores on trading-session offsets."""
    rows: list[dict] = []
    required = lookback_days + skip_days
    for observation_date in observation_dates:
        try:
            position = closes.index.get_loc(observation_date)
        except KeyError as exc:
            raise ValueError(f"{observation_date} is not in the trading calendar") from exc
        if not isinstance(position, int) or position < required:
            continue

        start = closes.iloc[position - required]
        end = closes.iloc[position - skip_days]
        scores = end / start - 1.0
        path_coverage = closes.iloc[position - required : position].notna().mean()
        endpoints_valid = start.gt(0) & end.gt(0) & scores.notna()
        valid = endpoints_valid & path_coverage.ge(0.90)
        ranks = scores[valid].rank(method="average", ascending=True)
        percentiles = ranks / len(ranks) if len(ranks) else ranks
        logger.debug(
            "momentum.cross_section_scored",
            observation_date=observation_date.isoformat(),
            candidates=len(closes.columns),
            valid=int(valid.sum()),
        )

        for instrument_id in closes.columns:
            is_valid = bool(valid[instrument_id])
            rows.append(
                {
                    "observation_date": observation_date,
                    "instrument_id": str(instrument_id),
                    "raw_score": float(scores[instrument_id]) if is_valid else None,
                    "average_rank": float(ranks[instrument_id]) if is_valid else None,
                    "rank_percentile": (
                        float(percentiles[instrument_id]) if is_valid else None
                    ),
                    "path_coverage": float(path_coverage[instrument_id]),
                    "factor_reason": (
                        None
                        if is_valid
                        else (
                            "insufficient_path_coverage"
                            if bool(endpoints_valid[instrument_id])
                            else "invalid_factor_endpoint"
                        )
                    ),
                }
            )
    return pd.DataFrame.from_records(rows)
