"""The normalised trading calendar written alongside each calendar publication.

Versioning is publication-scoped: every publication owns a complete set of
rows, so a revision never touches what an earlier snapshot resolves to.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.models.market_data import DataSnapshot, TradingCalendar
from tests.fakes import FakeAdapter

WEEK = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(7)]
OPEN_DAYS = [WEEK[0], WEEK[1], WEEK[2], WEEK[3], WEEK[4]]
CLOSED_DAYS = [WEEK[5], WEEK[6]]


def test_every_calendar_date_is_stored_open_and_closed_alike(make_workflow, session_factory):
    """Closed days are facts the consistency rule needs, not noise to drop."""
    adapter = FakeAdapter(trading_dates=OPEN_DAYS, half_days={WEEK[2]}, closed_dates=CLOSED_DAYS)
    workflow = make_workflow(adapter)
    run_id = workflow.start().id

    workflow.execute(run_id)

    with session_factory() as session:
        snapshot = session.scalar(select(DataSnapshot).where(DataSnapshot.sync_run_id == run_id))
        rows = session.scalars(
            select(TradingCalendar)
            .where(TradingCalendar.publication_id == snapshot.calendar_publication_id)
            .order_by(TradingCalendar.trade_date)
        ).all()

    assert [(row.trade_date, row.is_open, row.session) for row in rows] == [
        (WEEK[0], True, "full_day"),
        (WEEK[1], True, "full_day"),
        (WEEK[2], True, "half_day"),
        (WEEK[3], True, "full_day"),
        (WEEK[4], True, "full_day"),
        (WEEK[5], False, None),
        (WEEK[6], False, None),
    ]
    assert {row.market for row in rows} == {"TSE"}


def test_a_revision_writes_its_own_set_and_leaves_the_earlier_one_untouched(
    make_workflow, session_factory
):
    """What an earlier snapshot resolves through must not move under it.

    The source republishes the calendar every March, so a day flipping is a
    real event, not a hypothetical.
    """
    first = make_workflow(FakeAdapter(trading_dates=OPEN_DAYS, closed_dates=CLOSED_DAYS))
    first_run = first.start().id
    first.execute(first_run)

    # WEEK[4] is closed in the revision — it was open in the first publication.
    revised_open = [WEEK[0], WEEK[1], WEEK[2], WEEK[3]]
    second = make_workflow(FakeAdapter(trading_dates=revised_open, closed_dates=[*CLOSED_DAYS, WEEK[4]]))
    second_run = second.start().id
    second.execute(second_run)

    with session_factory() as session:
        snapshots = {
            row.sync_run_id: row.calendar_publication_id
            for row in session.scalars(select(DataSnapshot)).all()
        }

        def open_state(publication_id, trade_date):
            return session.scalar(
                select(TradingCalendar.is_open).where(
                    TradingCalendar.publication_id == publication_id,
                    TradingCalendar.trade_date == trade_date,
                )
            )

        assert snapshots[first_run] != snapshots[second_run]
        assert open_state(snapshots[first_run], WEEK[4]) is True
        assert open_state(snapshots[second_run], WEEK[4]) is False
