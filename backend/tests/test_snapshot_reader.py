"""Snapshot resolution by publish sequence (design §9.1, acceptance §18.3)."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.models.market_data import BarRecord, BarVersion, DataSnapshot
from app.services import snapshot_reader
from tests.fakes import FakeAdapter, bar_row

DATES = [date(2024, 2, 1) + timedelta(days=offset) for offset in range(4)]
CODE = "13010"


def _run(make_workflow, close: float):
    adapter = FakeAdapter(trading_dates=DATES, bars=lambda d: [bar_row(CODE, d, close=close)])
    workflow = make_workflow(adapter, batch_size=2)
    run_id = workflow.start().id
    workflow.execute(run_id)
    return run_id


def test_a_snapshot_only_sees_observations_within_its_cutoff(make_workflow, session_factory):
    """§18.3 items 3 and 4: an existing snapshot keeps reading what it froze,
    even after a later run revises the same bars."""
    first_run = _run(make_workflow, close=10)
    second_run = _run(make_workflow, close=99)

    with session_factory() as session:
        first = session.scalar(select(DataSnapshot).where(DataSnapshot.sync_run_id == first_run))
        second = session.scalar(select(DataSnapshot).where(DataSnapshot.sync_run_id == second_run))
        assert second.bar_publish_sequence > first.bar_publish_sequence

        rows = snapshot_reader.read_snapshot_bars(session, first)
        assert {row[0].raw_close for row in rows} == {10}

        rows = snapshot_reader.read_snapshot_bars(session, second)
        assert {row[0].raw_close for row in rows} == {99}


def test_declared_coverage_matches_the_resolved_members(make_workflow, session_factory):
    """§18.3 item 6: no resolvable member may fall outside the declared range."""
    run_id = _run(make_workflow, close=10)

    with session_factory() as session:
        snapshot = session.scalar(select(DataSnapshot).where(DataSnapshot.sync_run_id == run_id))
        coverage = snapshot_reader.resolve_coverage(
            session, snapshot.source, snapshot.bar_publish_sequence
        )

        assert (coverage.start, coverage.end) == (snapshot.coverage_start, snapshot.coverage_end)
        assert coverage.member_count == len(DATES)
        assert (
            snapshot_reader.count_members_outside(
                session,
                snapshot.source,
                snapshot.bar_publish_sequence,
                coverage_start=snapshot.coverage_start,
                coverage_end=snapshot.coverage_end,
            )
            == 0
        )


def test_coverage_accumulates_beyond_the_verified_window(make_workflow, session_factory):
    """§18.3 item 5: bars saved earlier stay readable when the source window
    rolls forward, but `verified` only names what this run re-checked."""
    _run(make_workflow, close=10)

    later_dates = [DATES[-1] + timedelta(days=offset) for offset in range(1, 3)]
    adapter = FakeAdapter(
        trading_dates=later_dates, bars=lambda d: [bar_row(CODE, d, close=12)]
    )
    workflow = make_workflow(adapter, batch_size=2)
    second_run = workflow.start().id
    workflow.execute(second_run)

    with session_factory() as session:
        snapshot = session.scalar(select(DataSnapshot).where(DataSnapshot.sync_run_id == second_run))

        # Verified names only the freshly re-checked window...
        assert (snapshot.verified_start, snapshot.verified_end) == (later_dates[0], later_dates[-1])
        # ...while coverage still spans the older bars this run never fetched.
        assert snapshot.coverage_start == DATES[0]
        assert snapshot.coverage_end == later_dates[-1]

        rows = snapshot_reader.read_snapshot_bars(session, snapshot)
        assert len(rows) == len(DATES) + len(later_dates)
