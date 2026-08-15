"""Calendar discovery and plan freezing (design §10.1/§10.2, acceptance §18.1).

The plan is frozen in the same transaction that publishes the calendar, so the
only reachable checkpoints are "not started", "staging, no plan" and
"published, complete plan". Everything here pins one of those down.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models.market_data import (
    DataSnapshot,
    EndpointPublication,
    PublicationStatus,
    RawSourcePage,
    SyncBatch,
    SyncRun,
    SyncRunStatus,
    SyncTargetDate,
    publish_sequence_seq,
)
from app.services.jquants_sync_workflow import SyncInvariantError
from tests.fakes import FakeAdapter

DATES = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(6)]


def _calendar_payload() -> dict:
    return {"data": [{"Date": item.isoformat(), "HolDiv": "1"} for item in DATES]}


def test_calendar_publication_and_plan_are_committed_together(make_workflow, session_factory):
    """§18.1 item 6: no half-plan state is observable after success."""
    adapter = FakeAdapter(trading_dates=DATES)
    workflow = make_workflow(adapter, batch_size=2)
    run_id = workflow.start().id
    workflow.execute(run_id)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        calendar = session.scalar(
            select(EndpointPublication).where(
                EndpointPublication.sync_run_id == run_id,
                EndpointPublication.endpoint == "markets/calendar",
            )
        )
        targets = session.scalar(
            select(func.count()).select_from(SyncTargetDate).where(SyncTargetDate.sync_run_id == run_id)
        )
        batches = session.scalar(
            select(func.count()).select_from(SyncBatch).where(SyncBatch.sync_run_id == run_id)
        )

    assert calendar.status == PublicationStatus.PUBLISHED
    assert calendar.publish_sequence is not None
    assert run.plan_fingerprint is not None
    assert targets == len(DATES) == run.target_dates
    assert batches == 3 == run.total_batches


def test_calendar_failure_leaves_neither_a_publication_nor_a_plan(make_workflow, session_factory):
    """The other half of §18.1 item 6."""

    class Broken(FakeAdapter):
        def fetch_calendar(self):
            raise RuntimeError("calendar endpoint down")

    workflow = make_workflow(Broken(trading_dates=DATES), batch_size=2)
    run_id = workflow.start().id

    with pytest.raises(RuntimeError):
        workflow.execute(run_id)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        published = session.scalar(
            select(func.count())
            .select_from(EndpointPublication)
            .where(
                EndpointPublication.sync_run_id == run_id,
                EndpointPublication.status == PublicationStatus.PUBLISHED,
            )
        )
        targets = session.scalar(
            select(func.count()).select_from(SyncTargetDate).where(SyncTargetDate.sync_run_id == run_id)
        )

    assert run.status == SyncRunStatus.FAILED, "nothing was published, so this is not partial"
    assert published == 0
    assert run.plan_fingerprint is None
    assert targets == 0


def test_staging_calendar_is_abandoned_and_refetched_on_recovery(make_workflow, session_factory):
    """§18.1 item 7: a staging attempt cannot be continued in place, because
    there is no way to prove every page arrived."""
    adapter = FakeAdapter(trading_dates=DATES)
    workflow = make_workflow(adapter, batch_size=2)
    run_id = workflow.start().id

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        session.add(
            EndpointPublication(
                sync_run_id=run_id,
                created_by_task_id=run.task_id,
                created_by_task_attempt=1,
                endpoint="markets/calendar",
                scope_ordinal=0,
                attempt=1,
                status=PublicationStatus.STAGING,
                adapter_version="test",
            )
        )
        session.commit()

    workflow.execute(run_id)

    assert adapter.calendar_requests == 1, "the abandoned attempt must be refetched in full"
    with session_factory() as session:
        attempts = session.scalars(
            select(EndpointPublication)
            .where(
                EndpointPublication.sync_run_id == run_id,
                EndpointPublication.endpoint == "markets/calendar",
            )
            .order_by(EndpointPublication.attempt)
        ).all()

    assert [item.attempt for item in attempts] == [1, 2]
    assert attempts[0].status == PublicationStatus.FAILED
    assert attempts[0].publish_sequence is None
    assert attempts[1].status == PublicationStatus.PUBLISHED


def test_published_calendar_without_a_plan_is_rebuilt_from_raw_pages(
    make_workflow, session_factory
):
    """§18.1 item 9: legacy compatibility. The stored pages are that run's
    date-discovery fact, so re-requesting the calendar would silently change
    what the run means."""
    adapter = FakeAdapter(trading_dates=DATES)
    workflow = make_workflow(adapter, batch_size=2)
    run_id = workflow.start().id

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        publication = EndpointPublication(
            sync_run_id=run_id,
            created_by_task_id=run.task_id,
            created_by_task_attempt=1,
            endpoint="markets/calendar",
            scope_ordinal=0,
            attempt=1,
            status=PublicationStatus.PUBLISHED,
            publish_sequence=session.scalar(select(publish_sequence_seq.next_value())),
            adapter_version="test",
        )
        session.add(publication)
        session.flush()
        session.add(
            RawSourcePage(
                publication_id=publication.id,
                page_index=0,
                payload=_calendar_payload(),
                content_hash="x" * 64,
                expires_at=run.created_at + timedelta(days=90),
            )
        )
        session.commit()

    outcome = workflow.execute(run_id)

    assert outcome.status == SyncRunStatus.SUCCEEDED
    assert adapter.calendar_requests == 0, "the plan must come from the stored pages"
    assert adapter.bar_requests == DATES


def test_published_calendar_without_pages_refuses_to_guess(make_workflow, session_factory):
    adapter = FakeAdapter(trading_dates=DATES)
    workflow = make_workflow(adapter, batch_size=2)
    run_id = workflow.start().id

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        session.add(
            EndpointPublication(
                sync_run_id=run_id,
                created_by_task_id=run.task_id,
                created_by_task_attempt=1,
                endpoint="markets/calendar",
                scope_ordinal=0,
                attempt=1,
                status=PublicationStatus.PUBLISHED,
                publish_sequence=session.scalar(select(publish_sequence_seq.next_value())),
                adapter_version="test",
            )
        )
        session.commit()

    with pytest.raises(SyncInvariantError):
        workflow.execute(run_id)
    assert adapter.calendar_requests == 0


def test_target_dates_without_a_fingerprint_are_rejected(make_workflow, session_factory):
    """§18.1 item 10, the inverse contradiction: a plan exists but nothing
    froze it."""
    adapter = FakeAdapter(trading_dates=DATES)
    workflow = make_workflow(adapter, batch_size=2)
    run_id = workflow.start().id
    workflow.execute(run_id)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        run.plan_fingerprint = None
        run.status = SyncRunStatus.PARTIAL_FAILED
        session.commit()

    with pytest.raises(SyncInvariantError):
        workflow.resume(run_id)


def test_empty_full_reconcile_fails_without_advancing_the_watermark(
    make_workflow, session_factory
):
    """§18.1 item 11 for the reconcile path, and item 13's shared rule: a
    non-empty plan whose batches all return zero rows is judged the same way."""
    first = FakeAdapter(trading_dates=DATES)
    workflow = make_workflow(first, batch_size=2)
    workflow.execute(workflow.start().id)

    with session_factory() as session:
        # Age the snapshot so the next run is due for a full reconcile.
        snapshot = session.scalar(select(DataSnapshot))
        snapshot.created_at = snapshot.created_at - timedelta(days=45)
        session.commit()

    empty = FakeAdapter(trading_dates=DATES, bars=lambda _: [])
    workflow = make_workflow(empty, batch_size=2)
    run_id = workflow.start().id

    with pytest.raises(Exception):
        workflow.execute(run_id)

    view = workflow.inspect(run_id)
    assert view.mode.value == "full_reconcile"
    assert view.error_code == "empty_full_reconcile_plan"
    assert view.resumable is False
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DataSnapshot)) == 1
