"""A scriptable stand-in for the J-Quants HTTP adapter.

Tests drive sync behaviour through the same interface the real adapter
exposes, so the workflow under test is the production one — only the network
is replaced.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from app.integrations.jquants import FetchResult, JQuantsError


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
        bars: Callable[[date], list[dict[str, Any]]] | None = None,
        master_rows: list[dict[str, Any]] | None = None,
        fail_dates: set[date] | None = None,
        fail_master: bool = False,
        persistent_failure: bool = True,
    ) -> None:
        self.trading_dates = trading_dates
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
        rows = [{"Date": item.isoformat(), "HolDiv": "1"} for item in self.trading_dates]
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
