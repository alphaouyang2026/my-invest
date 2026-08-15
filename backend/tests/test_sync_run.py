"""End-to-end sync runs: batching, publication atomicity, snapshot activation.

Covers acceptance §18.1 (continuous batches), §18.3 (snapshots) and the
failure/resume paths of §18.5.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models.market_data import (
    BarRecord,
    BarVersion,
    CurrentBar,
    DataSnapshot,
    DataSnapshotHead,
    EndpointPublication,
    PublicationStatus,
    SyncBatch,
    SyncBatchStatus,
    SyncMode,
    SyncRun,
    SyncRunStatus,
)
from app.services.jquants_sync_workflow import SyncInvariantError
from tests.fakes import FakeAdapter, bar_row

JANUARY = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(12)]


def _bars_publications(session, run_id: uuid.UUID) -> list[EndpointPublication]:
    return list(
        session.scalars(
            select(EndpointPublication)
            .where(
                EndpointPublication.sync_run_id == run_id,
                EndpointPublication.endpoint == "equities/bars/daily",
            )
            .order_by(EndpointPublication.scope_ordinal, EndpointPublication.attempt)
        ).all()
    )


def test_twelve_dates_publish_as_three_batch_generations(make_workflow, session_factory):
    """§18.1 item 1: one run, three sequential bars publications."""
    adapter = FakeAdapter(trading_dates=JANUARY)
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id

    outcome = workflow.execute(run_id)

    assert outcome.status == SyncRunStatus.SUCCEEDED
    assert outcome.mode == SyncMode.INITIAL
    assert adapter.bar_requests == JANUARY

    with session_factory() as session:
        published = [
            item for item in _bars_publications(session, run_id)
            if item.status == PublicationStatus.PUBLISHED
        ]
        assert len(published) == 3
        sequences = [item.publish_sequence for item in published]
        assert sequences == sorted(sequences) and len(set(sequences)) == 3

        view = workflow.inspect(run_id)
        assert (view.total_batches, view.completed_batches) == (3, 3)
        assert view.processed_dates == 12


def test_successful_run_creates_one_snapshot_and_moves_the_head(make_workflow, session_factory):
    """§18.3 item 2."""
    adapter = FakeAdapter(trading_dates=JANUARY)
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id

    outcome = workflow.execute(run_id)

    with session_factory() as session:
        snapshot = session.scalar(select(DataSnapshot).where(DataSnapshot.sync_run_id == run_id))
        head = session.get(DataSnapshotHead, "jquants")

        assert snapshot.id == outcome.snapshot_id
        assert head.snapshot_id == snapshot.id
        assert (snapshot.coverage_start, snapshot.coverage_end) == (JANUARY[0], JANUARY[-1])
        assert (snapshot.verified_start, snapshot.verified_end) == (JANUARY[0], JANUARY[-1])
        assert snapshot.bar_publish_sequence == max(
            item.publish_sequence
            for item in _bars_publications(session, run_id)
            if item.publish_sequence is not None
        )


def test_failed_second_batch_keeps_the_first_and_creates_no_snapshot(make_workflow, session_factory):
    """§18.3 item 1: early batches stay official, but a partial run must not
    become a readable research snapshot."""
    adapter = FakeAdapter(trading_dates=JANUARY, fail_dates={JANUARY[6]})
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id

    with pytest.raises(Exception):
        workflow.execute(run_id)

    with session_factory() as session:
        view = workflow.inspect(run_id)
        assert view.status == SyncRunStatus.PARTIAL_FAILED
        assert view.completed_batches == 1

        batches = session.scalars(
            select(SyncBatch).where(SyncBatch.sync_run_id == run_id).order_by(SyncBatch.ordinal)
        ).all()
        assert batches[0].status == SyncBatchStatus.PUBLISHED
        assert batches[1].status == SyncBatchStatus.FAILED

        # The first batch's dates are readable now...
        current = session.scalar(
            select(func.count())
            .select_from(CurrentBar)
            .join(BarRecord, BarRecord.id == CurrentBar.bar_record_id)
            .where(BarRecord.trade_date.in_(JANUARY[:5]))
        )
        assert current == 5
        # ...but nothing was frozen for backtests.
        assert session.scalar(select(func.count()).select_from(DataSnapshot)) == 0
        assert session.get(DataSnapshotHead, "jquants") is None


def test_resume_continues_from_the_first_unpublished_batch(make_workflow, session_factory):
    """§18.1 item 8 and §18.5 item 3: the frozen plan is reused and the
    calendar is never requested twice."""
    adapter = FakeAdapter(trading_dates=JANUARY, fail_dates={JANUARY[6]})
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id

    with pytest.raises(Exception):
        workflow.execute(run_id)
    assert adapter.calendar_requests == 1

    adapter.fail_dates.clear()
    workflow.resume(run_id)
    outcome = workflow.execute(run_id)

    assert outcome.status == SyncRunStatus.SUCCEEDED
    assert adapter.calendar_requests == 1, "resume must not re-request the calendar"
    # Batch 0 was already published, so its dates are not fetched again.
    assert adapter.bar_requests.count(JANUARY[0]) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DataSnapshot)) == 1


def test_empty_initial_run_fails_without_a_snapshot(make_workflow, session_factory):
    """§18.1 item 11: an empty first import is indistinguishable from a broken
    source, so it must not succeed."""
    adapter = FakeAdapter(trading_dates=JANUARY, bars=lambda _: [])
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id

    with pytest.raises(Exception):
        workflow.execute(run_id)

    view = workflow.inspect(run_id)
    assert view.status == SyncRunStatus.PARTIAL_FAILED
    assert view.error_code == "empty_initial_bars"
    assert view.resumable is False
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DataSnapshot)) == 0


def test_empty_incremental_inherits_the_current_head(make_workflow, session_factory):
    """§18.3 item 7: a new snapshot id, but the same cutoff, coverage and
    master as the head it inherits from."""
    first = FakeAdapter(trading_dates=JANUARY)
    workflow = make_workflow(first, batch_size=5)
    first_run = workflow.start().id
    workflow.execute(first_run)

    empty = FakeAdapter(trading_dates=JANUARY, bars=lambda _: [])
    workflow = make_workflow(empty, batch_size=5)
    second_run = workflow.start().id
    outcome = workflow.execute(second_run)

    assert outcome.status == SyncRunStatus.NO_CHANGE
    with session_factory() as session:
        previous = session.scalar(select(DataSnapshot).where(DataSnapshot.sync_run_id == first_run))
        inherited = session.scalar(select(DataSnapshot).where(DataSnapshot.sync_run_id == second_run))
        head = session.get(DataSnapshotHead, "jquants")

        assert inherited.id != previous.id
        assert outcome.snapshot_id == inherited.id
        assert head.snapshot_id == inherited.id
        assert inherited.bar_publish_sequence == previous.bar_publish_sequence
        assert inherited.coverage_start == previous.coverage_start
        assert inherited.coverage_end == previous.coverage_end
        assert inherited.master_snapshot_id == previous.master_snapshot_id
        assert empty.master_requests == 0, "an empty incremental must not fetch master"


def test_bars_succeed_but_master_failure_leaves_a_resumable_partial(make_workflow, session_factory):
    """§18.5 item 2: recovery retries only the master phase."""
    adapter = FakeAdapter(trading_dates=JANUARY, fail_master=True)
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id

    with pytest.raises(Exception):
        workflow.execute(run_id)

    view = workflow.inspect(run_id)
    assert view.status == SyncRunStatus.PARTIAL_FAILED
    assert view.completed_batches == 3

    adapter.fail_master = False
    bar_requests_before = len(adapter.bar_requests)
    workflow.resume(run_id)
    outcome = workflow.execute(run_id)

    assert outcome.status == SyncRunStatus.SUCCEEDED
    assert len(adapter.bar_requests) == bar_requests_before, "bars must not be re-fetched"
    assert adapter.calendar_requests == 1


def test_tampered_plan_fingerprint_blocks_automatic_continuation(make_workflow, session_factory):
    """§18.1 item 10."""
    adapter = FakeAdapter(trading_dates=JANUARY, fail_dates={JANUARY[6]})
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id
    with pytest.raises(Exception):
        workflow.execute(run_id)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        run.plan_fingerprint = "0" * 64
        session.commit()

    with pytest.raises(SyncInvariantError):
        workflow.resume(run_id)
