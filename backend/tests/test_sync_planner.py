"""Planning rules (design §10, acceptance §18.1 items 1-4)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.market_data import SyncMode
from app.services.sync_planner import (
    choose_target_dates,
    chunk_target_dates,
    plan_fingerprint,
)

NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)
VISIBLE_DATES = [date(2026, 4, 1), date(2026, 4, 15), date(2026, 5, 1)]


def test_first_sync_takes_every_visible_date() -> None:
    plan = choose_target_dates(
        VISIBLE_DATES, snapshot_coverage_end=None, last_full_snapshot_at=None, now=NOW
    )

    assert plan.mode == SyncMode.INITIAL
    assert plan.target_dates == VISIBLE_DATES


def test_full_reconcile_is_due_after_thirty_days() -> None:
    plan = choose_target_dates(
        VISIBLE_DATES,
        snapshot_coverage_end=date(2026, 5, 1),
        last_full_snapshot_at=NOW - timedelta(days=31),
        now=NOW,
    )

    assert plan.mode == SyncMode.FULL_RECONCILE
    assert plan.target_dates == VISIBLE_DATES


def test_recent_full_reconcile_falls_back_to_the_overlap_window() -> None:
    plan = choose_target_dates(
        VISIBLE_DATES,
        snapshot_coverage_end=date(2026, 5, 10),
        last_full_snapshot_at=NOW - timedelta(days=5),
        now=NOW,
    )

    assert plan.mode == SyncMode.INCREMENTAL
    # 30 calendar days back from 2026-05-10 starts at 2026-04-10, dropping 04-01.
    assert plan.target_dates == [date(2026, 4, 15), date(2026, 5, 1)]


def test_incremental_window_covers_every_date_in_the_overlap() -> None:
    """The whole 30-day window is re-fetched, not just the earliest few —
    otherwise a run would stall on the same dates forever (§18.1 item 4)."""
    visible = [date(2024, 1, day) for day in range(2, 12)]

    plan = choose_target_dates(
        visible,
        snapshot_coverage_end=date(2024, 1, 11),
        last_full_snapshot_at=datetime(2024, 1, 4, tzinfo=timezone.utc),
        now=datetime(2024, 1, 12, tzinfo=timezone.utc),
    )

    assert plan.mode == SyncMode.INCREMENTAL
    assert plan.target_dates == visible


def test_chunking_is_independent_of_the_chosen_dates() -> None:
    dates = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(12)]

    chunks = chunk_target_dates(dates, batch_size=5)

    assert [len(chunk) for chunk in chunks] == [5, 5, 2]
    assert [item for chunk in chunks for item in chunk] == dates


def test_chunking_rejects_a_meaningless_batch_size() -> None:
    with pytest.raises(ValueError):
        chunk_target_dates([date(2024, 1, 1)], batch_size=0)


def test_fingerprint_is_order_sensitive_and_stable() -> None:
    dates = [date(2024, 1, 1), date(2024, 1, 2)]

    assert plan_fingerprint(dates) == plan_fingerprint(list(dates))
    assert plan_fingerprint(dates) != plan_fingerprint(list(reversed(dates)))
    assert plan_fingerprint([]) == plan_fingerprint([])
