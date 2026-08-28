from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from app.services.stock_pool import DEFAULT_POLICY, policy_fingerprint


FACTOR_DEFINITION = "momentum_6_1"
FACTOR_VERSION = "1"
DAILY_LABEL_DEFINITION = "next_open_to_sixth_following_open"
WEEKLY_LABEL_DEFINITION = "next_rebalance_open_to_next_rebalance_open"
MIN_COVERAGE = 0.90
MIN_VALID_SECURITIES = 100
GROUP_COUNT = 5


@dataclass(frozen=True)
class ResearchDefinition:
    """The immutable, canonical meaning of one factor experiment."""

    data_snapshot_id: UUID
    observation_start: date
    observation_end: date
    lookback_days: int = 126
    skip_days: int = 21
    stock_pool_policy_fingerprint: str = policy_fingerprint(DEFAULT_POLICY)

    def __post_init__(self) -> None:
        if self.observation_start > self.observation_end:
            raise ValueError("observation_start must not be after observation_end")
        if self.lookback_days < 1:
            raise ValueError("lookback_days must be positive")
        if self.skip_days < 1:
            raise ValueError("skip_days must be positive")

    @property
    def required_history_days(self) -> int:
        return self.lookback_days + self.skip_days

    @property
    def canonical_payload(self) -> dict[str, Any]:
        return {
            "data_snapshot_id": str(self.data_snapshot_id),
            "factor": {
                "name": FACTOR_DEFINITION,
                "version": FACTOR_VERSION,
                "lookback_days": self.lookback_days,
                "skip_days": self.skip_days,
            },
            "observation_range": {
                "start": self.observation_start.isoformat(),
                "end": self.observation_end.isoformat(),
            },
            "stock_pool_policy_fingerprint": self.stock_pool_policy_fingerprint,
            "labels": {
                "daily": DAILY_LABEL_DEFINITION,
                "weekly": WEEKLY_LABEL_DEFINITION,
            },
            "evaluation": {
                "factor_coverage": MIN_COVERAGE,
                "label_coverage": MIN_COVERAGE,
                "min_valid_securities": MIN_VALID_SECURITIES,
                "group_count": GROUP_COUNT,
                "ic": "pearson",
                "rank_ic": "spearman_average_rank",
                "icir": "mean_over_sample_std_unannualized",
            },
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.canonical_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
