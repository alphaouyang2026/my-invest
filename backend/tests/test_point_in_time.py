"""Validity intervals derived from the observation trail."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.models.market_data import BarRecord
from app.services.point_in_time import version_intervals
from tests.fakes import FakeAdapter, bar_row

DATES = [date(2024, 3, 1), date(2024, 3, 4)]
CODE = "13010"


def _run(make_workflow, close: float):
    adapter = FakeAdapter(trading_dates=DATES, bars=lambda d: [bar_row(CODE, d, close=close)])
    workflow = make_workflow(adapter)
    workflow.execute(workflow.start().id)


def test_a_reverted_version_holds_two_disjoint_intervals(make_workflow, session_factory):
    """Why the interval is derived instead of stored.

    A -> B -> A reuses the original version row, so that one row was the
    current belief over two separate stretches. A single effective_from /
    effective_to pair on the row could only ever describe one of them.
    """
    _run(make_workflow, close=10)
    _run(make_workflow, close=11)
    _run(make_workflow, close=10)

    with session_factory() as session:
        record = session.scalar(select(BarRecord).where(BarRecord.trade_date == DATES[0]))
        intervals = version_intervals(session, record.id)

    assert len(intervals) == 3
    first, middle, last = intervals

    assert first.bar_version_id == last.bar_version_id, "the revert reuses the original row"
    assert first.bar_version_id != middle.bar_version_id

    # Every superseded belief is closed; only the standing one stays open.
    assert first.effective_to is not None
    assert middle.effective_to is not None
    assert last.effective_to is None

    # Contiguous: one belief ends exactly where the next begins.
    assert first.to_sequence == middle.from_sequence
    assert middle.to_sequence == last.from_sequence
    assert first.from_sequence < middle.from_sequence < last.from_sequence


def test_reobserving_the_same_content_extends_rather_than_splits(make_workflow, session_factory):
    """An unchanged row is the same belief being confirmed, not a new one."""
    _run(make_workflow, close=10)
    _run(make_workflow, close=10)

    with session_factory() as session:
        record = session.scalar(select(BarRecord).where(BarRecord.trade_date == DATES[0]))
        intervals = version_intervals(session, record.id)

    assert len(intervals) == 1
    assert intervals[0].effective_to is None
