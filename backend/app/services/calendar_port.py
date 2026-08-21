"""Trading-calendar queries, bound to one published calendar.

A port is constructed over a `calendar_publication_id`, never over "the
current calendar". Stock pools and backtests ask historical questions, so a
port that followed the latest publication would let a March calendar revision
change what a finished backtest saw — the one thing snapshots exist to prevent.
`DataSnapshot.calendar_publication_id` is the pointer callers pass in; the
quality pass, which runs before its snapshot exists, passes the id from the
run's frozen plan instead.

Every query is total over the calendar's coverage and refuses outside it.
Extrapolating "weekdays are open" would invent trading days on Japanese
holidays, and a shortened window silently shortens every lookback built on it.
"""

from __future__ import annotations

import bisect
from datetime import date
from typing import Callable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.market_data import TradingCalendar
from app.services.calendar_normalization import MARKET_TSE, CalendarDay


class CalendarCoverageError(RuntimeError):
    """The calendar cannot answer: the date, or the window, falls outside it."""


class CalendarPort(Protocol):
    def is_open(self, day: date) -> bool: ...

    def previous_open(self, day: date) -> date: ...

    def next_open(self, day: date) -> date: ...

    def window_back(self, end: date, count: int) -> list[date]: ...

    def open_days_between(self, start: date, end: date) -> list[date]: ...


class _CalendarQueries:
    """Query logic shared by every port, over an ordered list of days.

    Both implementations differ only in where the days come from, so keeping
    the semantics in one place is what lets the fake stay a faithful stand-in
    rather than a second, subtly different calendar.
    """

    def __init__(self, days: list[CalendarDay]) -> None:
        ordered = sorted(days, key=lambda item: item.trade_date)
        self._is_open = {day.trade_date: day.is_open for day in ordered}
        self._open_days = [day.trade_date for day in ordered if day.is_open]
        self._first = ordered[0].trade_date if ordered else None
        self._last = ordered[-1].trade_date if ordered else None

    def _require_covered(self, day: date) -> None:
        if self._first is None or not (self._first <= day <= self._last):
            raise CalendarCoverageError(
                f"{day.isoformat()} is outside the calendar's coverage "
                f"({self._first} .. {self._last})"
            )

    def is_open(self, day: date) -> bool:
        self._require_covered(day)
        return self._is_open.get(day, False)

    def previous_open(self, day: date) -> date:
        self._require_covered(day)
        index = bisect.bisect_left(self._open_days, day)
        if index == 0:
            raise CalendarCoverageError(f"No open trading day before {day.isoformat()} in coverage")
        return self._open_days[index - 1]

    def next_open(self, day: date) -> date:
        self._require_covered(day)
        index = bisect.bisect_right(self._open_days, day)
        if index >= len(self._open_days):
            raise CalendarCoverageError(f"No open trading day after {day.isoformat()} in coverage")
        return self._open_days[index]

    def window_back(self, end: date, count: int) -> list[date]:
        """The `count` open days before `end`, half-open `[.., end)`.

        `end` is excluded because it is the decision day: letting its own price
        into the signal computed for it is look-ahead bias.
        """
        if count < 0:
            raise ValueError("count must not be negative")
        self._require_covered(end)
        stop = bisect.bisect_left(self._open_days, end)
        if stop < count:
            raise CalendarCoverageError(
                f"Only {stop} open trading days precede {end.isoformat()} in coverage; {count} requested"
            )
        return self._open_days[stop - count : stop]

    def open_days_between(self, start: date, end: date) -> list[date]:
        self._require_covered(start)
        self._require_covered(end)
        left = bisect.bisect_left(self._open_days, start)
        right = bisect.bisect_right(self._open_days, end)
        return self._open_days[left:right]


def load_calendar_days(
    session: Session, publication_id, *, market: str = MARKET_TSE
) -> list[CalendarDay]:
    rows = session.scalars(
        select(TradingCalendar)
        .where(
            TradingCalendar.publication_id == publication_id,
            TradingCalendar.market == market,
        )
        .order_by(TradingCalendar.trade_date)
    ).all()
    if not rows:
        raise CalendarCoverageError(f"Calendar publication {publication_id} has no {market} days")
    return [
        CalendarDay(
            trade_date=row.trade_date,
            hol_div=row.hol_div,
            is_open=row.is_open,
            session=row.session,
        )
        for row in rows
    ]


class DbCalendarPort(_CalendarQueries):
    """Reads the complete calendar owned by one publication.

    Loaded once per port: a calendar is a few hundred rows, and re-querying per
    call would make a 126-day window 126 round trips.
    """

    def __init__(
        self,
        sessions: Callable[[], Session],
        publication_id,
        *,
        market: str = MARKET_TSE,
    ) -> None:
        with sessions() as session:
            super().__init__(load_calendar_days(session, publication_id, market=market))


class SessionCalendarPort(_CalendarQueries):
    """The same port over a session the caller already holds.

    Exists because a request handler is given its session by dependency
    injection and has no factory to hand over; the alternative — opening a
    second connection inside a request that already has one — is how a request
    ends up reading across two transactions.
    """

    def __init__(self, session: Session, publication_id, *, market: str = MARKET_TSE) -> None:
        super().__init__(load_calendar_days(session, publication_id, market=market))
