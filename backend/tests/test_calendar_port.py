"""CalendarPort, exercised through both implementations.

Every test here runs against the Postgres-backed port and an independently
written in-memory one. Two things fall out of that: the Protocol is provably
substitutable rather than substitutable in principle, and the naive
implementation acts as an oracle for the optimised bisect logic — if they ever
disagree, one of them is wrong.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.calendar_normalization import CalendarDay, normalize_calendar
from app.services.calendar_port import CalendarCoverageError, DbCalendarPort
from tests.fakes import FakeAdapter, InMemoryCalendarPort

# Mon 2024-01-01 .. Sun 2024-01-14, weekends closed, 2024-01-10 a half day.
WEEK_ROWS = []
for _offset in range(14):
    _day = date(2024, 1, 1) + timedelta(days=_offset)
    if _day.weekday() >= 5:
        _code = "0"
    elif _day == date(2024, 1, 10):
        _code = "2"
    else:
        _code = "1"
    WEEK_ROWS.append({"Date": _day.isoformat(), "HolDiv": _code})

CALENDAR: list[CalendarDay] = normalize_calendar(WEEK_ROWS)
OPEN_DAYS = [day.trade_date for day in CALENDAR if day.is_open]


@pytest.fixture
def in_memory_port() -> InMemoryCalendarPort:
    return InMemoryCalendarPort(CALENDAR)


@pytest.fixture
def db_port(make_workflow, session_factory) -> DbCalendarPort:
    """A port over a calendar published by a real sync run."""
    closed = [day.trade_date for day in CALENDAR if not day.is_open]
    half_days = {day.trade_date for day in CALENDAR if day.session == "half_day"}
    adapter = FakeAdapter(trading_dates=OPEN_DAYS, half_days=half_days, closed_dates=closed)
    workflow = make_workflow(adapter)
    run_id = workflow.start().id
    outcome = workflow.execute(run_id)

    from app.models.market_data import DataSnapshot

    with session_factory() as session:
        snapshot = session.get(DataSnapshot, outcome.snapshot_id)
        publication_id = snapshot.calendar_publication_id

    return DbCalendarPort(session_factory, publication_id)


@pytest.fixture(params=["in_memory", "db"])
def port(request, in_memory_port, db_port):
    return in_memory_port if request.param == "in_memory" else db_port


def test_is_open_reflects_the_calendar(port):
    assert port.is_open(date(2024, 1, 5)) is True  # Friday
    assert port.is_open(date(2024, 1, 6)) is False  # Saturday
    assert port.is_open(date(2024, 1, 10)) is True  # half day still trades


def test_previous_and_next_open_skip_closed_days(port):
    # 2024-01-06/07 is a weekend.
    assert port.next_open(date(2024, 1, 5)) == date(2024, 1, 8)
    assert port.previous_open(date(2024, 1, 8)) == date(2024, 1, 5)


def test_window_back_excludes_the_end_date(port):
    """Half-open [.., end): the decision day's own price must not enter the
    signal computed for it."""
    window = port.window_back(date(2024, 1, 10), 3)

    assert window == [date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 9)]


def test_window_back_raises_when_history_runs_short(port):
    """A truncated window silently shortens every momentum lookback that uses
    it, so refusing is the only safe answer."""
    with pytest.raises(CalendarCoverageError):
        port.window_back(date(2024, 1, 3), 50)


def test_queries_outside_coverage_raise_rather_than_extrapolate(port):
    """The Free calendar always ends ~12 weeks in the past; guessing that a
    weekday is open would invent trading days on Japanese holidays."""
    with pytest.raises(CalendarCoverageError):
        port.is_open(date(2025, 6, 2))

    with pytest.raises(CalendarCoverageError):
        port.next_open(date(2024, 1, 14))


def test_open_days_between_is_inclusive_of_both_ends(port):
    assert port.open_days_between(date(2024, 1, 4), date(2024, 1, 9)) == [
        date(2024, 1, 4),
        date(2024, 1, 5),
        date(2024, 1, 8),
        date(2024, 1, 9),
    ]
