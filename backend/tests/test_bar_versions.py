"""Bar version and publication semantics (acceptance §18.2)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.models.market_data import (
    BarObservationDisposition,
    BarQualityStatus,
    BarRecord,
    BarVersion,
    CurrentBar,
    EndpointPublication,
    PublicationBarObservation,
    PublicationStatus,
    SyncRunStatus,
)
from app.services.quality_rules import QualityRule
from tests.fakes import FakeAdapter, bar_row

DATES = [date(2024, 3, 1), date(2024, 3, 4)]
CODE = "13010"


def _record(session) -> BarRecord:
    return session.scalar(select(BarRecord).where(BarRecord.trade_date == DATES[0]))


def _run_once(make_workflow, close: float):
    adapter = FakeAdapter(trading_dates=DATES, bars=lambda d: [bar_row(CODE, d, close=close)])
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id
    outcome = workflow.execute(run_id)
    return workflow, run_id, outcome


def test_reverting_to_earlier_content_reuses_the_original_version(make_workflow, session_factory):
    """§18.2 item 2: A -> B -> A stores two versions, not three, and the third
    run points back at the original A."""
    _run_once(make_workflow, close=10)
    with session_factory() as session:
        original = session.get(CurrentBar, _record(session).id).bar_version_id

    _run_once(make_workflow, close=11)
    with session_factory() as session:
        revised = session.get(CurrentBar, _record(session).id).bar_version_id
    assert revised != original

    _, third_run, outcome = _run_once(make_workflow, close=10)

    assert outcome.status == SyncRunStatus.SUCCEEDED
    with session_factory() as session:
        record = _record(session)
        versions = session.scalars(
            select(BarVersion).where(BarVersion.bar_record_id == record.id)
        ).all()
        assert len(versions) == 2, "identical content must not be stored twice"
        assert session.get(CurrentBar, record.id).bar_version_id == original

        # Every generation still recorded what it saw.
        observations = session.scalars(
            select(PublicationBarObservation)
            .where(PublicationBarObservation.bar_record_id == record.id)
        ).all()
        assert len(observations) == 3
        assert observations[-1].disposition == BarObservationDisposition.REVERTED


def test_a_reused_version_still_carries_its_own_quality_verdict(make_workflow, session_factory):
    """The reason row-local quality lives on BarVersion at all.

    A defective bar, corrected, then reverted: the third run reuses the
    original row, so its verdict has to have been a property of the content
    rather than of the moment it was judged.
    """

    def defective(_: date) -> list[dict]:
        row = bar_row(CODE, DATES[0], close=10)
        row["C"] = None  # critical field missing
        return [row]

    def healthy(trade_date: date) -> list[dict]:
        return [bar_row(CODE, trade_date, close=10)]

    for bars in (defective, healthy, defective):
        adapter = FakeAdapter(trading_dates=[DATES[0]], bars=bars)
        workflow = make_workflow(adapter, batch_size=5)
        workflow.execute(workflow.start().id)

    with session_factory() as session:
        record = _record(session)
        versions = session.scalars(
            select(BarVersion).where(BarVersion.bar_record_id == record.id)
        ).all()
        current = session.get(BarVersion, session.get(CurrentBar, record.id).bar_version_id)

    assert len(versions) == 2, "the revert must reuse the original row"
    assert current.quality_status is BarQualityStatus.UNTRADABLE
    assert QualityRule.MISSING_CRITICAL_FIELD.value in current.quality_rules


def test_unchanged_rows_are_observed_without_moving_the_pointer(make_workflow, session_factory):
    """§18.2 item 3."""
    _run_once(make_workflow, close=10)
    with session_factory() as session:
        before = session.get(CurrentBar, _record(session).id).bar_version_id

    _, second_run, outcome = _run_once(make_workflow, close=10)

    assert outcome.status == SyncRunStatus.NO_CHANGE
    with session_factory() as session:
        record = _record(session)
        assert session.get(CurrentBar, record.id).bar_version_id == before

        second_observation = session.scalar(
            select(PublicationBarObservation)
            .join(
                EndpointPublication,
                EndpointPublication.id == PublicationBarObservation.publication_id,
            )
            .where(
                EndpointPublication.sync_run_id == second_run,
                PublicationBarObservation.bar_record_id == record.id,
            )
        )
        assert second_observation is not None
        assert second_observation.disposition == BarObservationDisposition.UNCHANGED


def test_failed_publications_never_get_a_sequence_or_touch_current_bars(
    make_workflow, session_factory
):
    """§18.2 item 4 — also enforced by a CHECK constraint in the schema."""
    adapter = FakeAdapter(
        trading_dates=DATES, bars=lambda d: [bar_row(CODE, d)], fail_dates={DATES[1]}
    )
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id

    with pytest.raises(Exception):
        workflow.execute(run_id)

    with session_factory() as session:
        publications = session.scalars(
            select(EndpointPublication).where(EndpointPublication.sync_run_id == run_id)
        ).all()
        failed = [item for item in publications if item.status == PublicationStatus.FAILED]
        assert failed, "the bars attempt should be recorded as failed"
        assert all(item.publish_sequence is None for item in failed)

        # Nothing in this run reached CurrentBar: the batch never published.
        assert session.scalar(select(func.count()).select_from(CurrentBar)) == 0


def test_retrying_a_failed_attempt_leaves_the_old_generation_untouched(
    make_workflow, session_factory
):
    """§18.2 item 1: the retry is a new publication attempt, not an edit."""
    adapter = FakeAdapter(
        trading_dates=DATES, bars=lambda d: [bar_row(CODE, d)], fail_dates={DATES[1]}
    )
    workflow = make_workflow(adapter, batch_size=5)
    run_id = workflow.start().id
    with pytest.raises(Exception):
        workflow.execute(run_id)

    with session_factory() as session:
        first = session.scalar(
            select(EndpointPublication).where(
                EndpointPublication.sync_run_id == run_id,
                EndpointPublication.status == PublicationStatus.FAILED,
                EndpointPublication.endpoint == "equities/bars/daily",
            )
        )
        first_id, first_attempt = first.id, first.attempt

    adapter.fail_dates.clear()
    workflow.resume(run_id)
    workflow.execute(run_id)

    with session_factory() as session:
        original = session.get(EndpointPublication, first_id)
        assert original.status == PublicationStatus.FAILED
        assert original.publish_sequence is None

        published = session.scalar(
            select(EndpointPublication).where(
                EndpointPublication.sync_run_id == run_id,
                EndpointPublication.endpoint == "equities/bars/daily",
                EndpointPublication.status == PublicationStatus.PUBLISHED,
            )
        )
        assert published.id != first_id
        assert published.attempt == first_attempt + 1
