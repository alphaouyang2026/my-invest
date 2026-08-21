"""Source-integrity checks that have to happen while rows are being written.

These are the quality rules a later pass could not run, because the evidence
would already be gone by then: a duplicate business key is silently collapsed
by the observation upsert, and a bar dated outside the request would be filed
under whatever date it claims. Failing the endpoint keeps the dirty batch out
of the database entirely, which is stronger than recording a finding about it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.market_data import EndpointPublication, PublicationStatus
from tests.fakes import FakeAdapter, bar_row

DATES = [date(2024, 3, 1), date(2024, 3, 4)]
CODE = "13010"


def _no_bars_published(session, run_id) -> bool:
    published = session.scalars(
        select(EndpointPublication).where(
            EndpointPublication.sync_run_id == run_id,
            EndpointPublication.endpoint == "equities/bars/daily",
            EndpointPublication.status == PublicationStatus.PUBLISHED,
        )
    ).all()
    return not published


def test_a_duplicate_business_key_in_one_batch_fails_the_endpoint(make_workflow, session_factory):
    """Two rows for the same security and date contradict each other about what
    happened. The observation upsert would keep whichever arrived last, so the
    conflict has to be caught while both are still visible."""

    def duplicated(trade_date: date) -> list[dict]:
        return [
            bar_row(CODE, trade_date, close=10),
            bar_row(CODE, trade_date, close=11),
        ]

    adapter = FakeAdapter(trading_dates=DATES, bars=duplicated)
    workflow = make_workflow(adapter)
    run_id = workflow.start().id

    with pytest.raises(Exception):
        workflow.execute(run_id)

    with session_factory() as session:
        assert _no_bars_published(session, run_id)


def test_a_bar_dated_outside_the_request_fails_the_endpoint(make_workflow, session_factory):
    """This is the whole of the "time reversal" rule.

    Bars are fetched one date at a time, so a row carrying any other date —
    a future one included — means the request and the response no longer
    correspond, which is a systemic fault rather than dirty data. Because
    every requested date comes from the calendar, a future trade date cannot
    arrive any other way, so no separate futurity check is needed.
    """
    future = date.today() + timedelta(days=365)

    def misdated(trade_date: date) -> list[dict]:
        row = bar_row(CODE, trade_date)
        row["Date"] = future.isoformat()
        return [row]

    adapter = FakeAdapter(trading_dates=DATES, bars=misdated)
    workflow = make_workflow(adapter)
    run_id = workflow.start().id

    with pytest.raises(Exception):
        workflow.execute(run_id)

    with session_factory() as session:
        assert _no_bars_published(session, run_id)
