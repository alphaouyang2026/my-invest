"""Target-date planning, kept deliberately separate from batch chunking.

Choosing *what* to sync is a business rule driven by snapshot coverage;
slicing that list into batches is an execution detail driven by `SyncPolicy`.
Keeping them apart is why FULL_RECONCILE's date set is unaffected by batch
size (docs/design/jquants-continuous-batch-sync.md §10).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.models.market_data import SyncMode

FULL_RECONCILE_INTERVAL = timedelta(days=30)
INCREMENTAL_OVERLAP = timedelta(days=30)


@dataclass(frozen=True)
class SyncPlan:
    mode: SyncMode
    target_dates: list[date]

    @property
    def fingerprint(self) -> str:
        return plan_fingerprint(self.target_dates)


def choose_target_dates(
    visible_dates: list[date],
    *,
    snapshot_coverage_end: date | None,
    last_full_snapshot_at: datetime | None,
    now: datetime,
) -> SyncPlan:
    """Decide the run's mode and its complete, frozen target-date list.

    `snapshot_coverage_end` comes from DataSnapshotHead — never from partially
    advanced CurrentBar rows, which would let an abandoned run silently narrow
    the next plan.
    """
    ordered = sorted(set(visible_dates))

    if snapshot_coverage_end is None:
        return SyncPlan(SyncMode.INITIAL, ordered)

    if last_full_snapshot_at is None or now - last_full_snapshot_at >= FULL_RECONCILE_INTERVAL:
        return SyncPlan(SyncMode.FULL_RECONCILE, ordered)

    overlap_start = snapshot_coverage_end - INCREMENTAL_OVERLAP
    return SyncPlan(SyncMode.INCREMENTAL, [item for item in ordered if item >= overlap_start])


def chunk_target_dates(target_dates: list[date], *, batch_size: int) -> list[list[date]]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    return [target_dates[start : start + batch_size] for start in range(0, len(target_dates), batch_size)]


def plan_fingerprint(target_dates: list[date]) -> str:
    """Hash of the full ordered plan. Resume validates this before reusing a
    frozen plan, so a plan can never be silently re-derived mid-run."""
    encoded = "|".join(item.isoformat() for item in target_dates)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
