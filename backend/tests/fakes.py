"""A scriptable stand-in for the J-Quants HTTP adapter.

Tests drive sync behaviour through the same interface the real adapter
exposes, so the workflow under test is the production one — only the network
is replaced.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from app.integrations.jquants import FetchResult, JQuantsError
from app.services.calendar_normalization import CalendarDay
from app.services.calendar_port import CalendarCoverageError


class InMemoryCalendarPort:
    """A deliberately naive CalendarPort, implemented independently.

    It is an oracle, not a shortcut: the port tests run the same assertions
    against this list-scanning version and the production bisect one, so a
    disagreement means one of them is wrong. Sharing the query code would make
    that cross-check vacuous. It also lets later pool/window work be tested
    without standing up a sync run.
    """

    def __init__(self, days: list[CalendarDay]) -> None:
        self._days = sorted(days, key=lambda item: item.trade_date)
        self._open = [day.trade_date for day in self._days if day.is_open]

    def _check(self, day: date) -> None:
        if not self._days or not (self._days[0].trade_date <= day <= self._days[-1].trade_date):
            raise CalendarCoverageError(f"{day.isoformat()} is outside the calendar's coverage")

    def is_open(self, day: date) -> bool:
        self._check(day)
        return any(item.trade_date == day and item.is_open for item in self._days)

    def previous_open(self, day: date) -> date:
        self._check(day)
        earlier = [item for item in self._open if item < day]
        if not earlier:
            raise CalendarCoverageError(f"No open trading day before {day.isoformat()} in coverage")
        return earlier[-1]

    def next_open(self, day: date) -> date:
        self._check(day)
        later = [item for item in self._open if item > day]
        if not later:
            raise CalendarCoverageError(f"No open trading day after {day.isoformat()} in coverage")
        return later[0]

    def window_back(self, end: date, count: int) -> list[date]:
        if count < 0:
            raise ValueError("count must not be negative")
        self._check(end)
        earlier = [item for item in self._open if item < end]
        if len(earlier) < count:
            raise CalendarCoverageError(
                f"Only {len(earlier)} open trading days precede {end.isoformat()}; {count} requested"
            )
        return earlier[len(earlier) - count :]

    def open_days_between(self, start: date, end: date) -> list[date]:
        self._check(start)
        self._check(end)
        return [item for item in self._open if start <= item <= end]


def bar_row(code: str, trade_date: date, close: float = 10) -> dict[str, Any]:
    return {
        "Code": code,
        "Date": trade_date.isoformat(),
        "O": close,
        "H": close + 1,
        "L": close - 1,
        "C": close,
        "Vo": 100,
    }


class FakeAdapter:
    """Serves a fixed calendar plus per-date bars, with optional failures.

    `fail_dates` raises on the given trade dates; `fail_master` makes the
    master endpoint fail. Both are one-shot unless `persistent_failure` is set,
    which lets a test assert that a retry succeeds.
    """

    API_VERSION = "v2"
    ADAPTER_VERSION = "test"

    def __init__(
        self,
        *,
        trading_dates: list[date],
        half_days: set[date] | None = None,
        closed_dates: list[date] | None = None,
        bars: Callable[[date], list[dict[str, Any]]] | None = None,
        master_rows: list[dict[str, Any]] | None = None,
        fail_dates: set[date] | None = None,
        fail_master: bool = False,
        persistent_failure: bool = True,
    ) -> None:
        self.trading_dates = trading_dates
        self.half_days = set(half_days or ())
        self.closed_dates = list(closed_dates or ())
        self._bars = bars or (lambda d: [bar_row("13010", d)])
        self._master_rows = master_rows if master_rows is not None else [{"Code": "13010", "Mkt": "0111"}]
        self.fail_dates = set(fail_dates or ())
        self.fail_master = fail_master
        self._persistent = persistent_failure
        self.bar_requests: list[date] = []
        self.calendar_requests = 0
        self.master_requests = 0

    def fetch_calendar(self) -> FetchResult:
        self.calendar_requests += 1
        rows = [
            {"Date": item.isoformat(), "HolDiv": "2" if item in self.half_days else "1"}
            for item in self.trading_dates
        ]
        rows.extend({"Date": item.isoformat(), "HolDiv": "0"} for item in self.closed_dates)
        rows.sort(key=lambda row: row["Date"])
        return FetchResult(rows=rows, pages=[{"data": rows}])

    def fetch_daily_bars(self, trade_date: str) -> FetchResult:
        parsed = date.fromisoformat(trade_date)
        self.bar_requests.append(parsed)
        if parsed in self.fail_dates:
            if not self._persistent:
                self.fail_dates.discard(parsed)
            raise JQuantsError(f"daily bars unavailable for {trade_date}")
        rows = self._bars(parsed)
        return FetchResult(rows=rows, pages=[{"data": rows}])

    def fetch_master(self, as_of_date: str) -> FetchResult:
        self.master_requests += 1
        if self.fail_master:
            if not self._persistent:
                self.fail_master = False
            raise JQuantsError("master unavailable")
        return FetchResult(rows=list(self._master_rows), pages=[{"data": list(self._master_rows)}])
