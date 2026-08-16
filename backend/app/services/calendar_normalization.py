"""J-Quants calendar rows -> trading-calendar facts.

Kept apart from the sync workflow because deciding *what a date is* is a
source-vocabulary question, while deciding *which dates to fetch* is a
planning question (`sync_planner`). The workflow needs both and owns neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

FULL_DAY = "full_day"
HALF_DAY = "half_day"

# The source calendar has no market dimension; this system trades TSE cash
# equities only, and that is what its `is_open` is true of.
MARKET_TSE = "TSE"

# The four documented values of J-Quants V2 `HolDiv`, mapped to what they mean
# for TSE cash equities — the only market this system trades.
# https://jpx-jquants.com/ja/spec/mkt-cal/holiday-division
_HOLIDAY_DIVISIONS: dict[str, tuple[bool, str | None]] = {
    "0": (False, None),  # 非営業日
    "1": (True, FULL_DAY),  # 営業日
    "2": (True, HALF_DAY),  # 東証半日立会日
    # 非営業日(祝日取引あり) — OSE derivatives trade, TSE cash equities do not.
    "3": (False, None),
}


@dataclass(frozen=True)
class CalendarDay:
    trade_date: date
    hol_div: str
    is_open: bool
    session: str | None


def normalize_calendar(rows: list[dict[str, Any]]) -> list[CalendarDay]:
    days: dict[date, CalendarDay] = {}
    for row in rows:
        hol_div = str(row.get("HolDiv"))
        # An unrecognised code is treated as closed rather than raising: the
        # source can add a value at any time, and a crashed sync is a worse
        # answer than a date we didn't fetch. The raw code survives on the row
        # so the unknown value stays visible instead of being erased.
        is_open, session = _HOLIDAY_DIVISIONS.get(hol_div, (False, None))
        trade_date = date.fromisoformat(str(row["Date"]))
        day = CalendarDay(
            trade_date=trade_date,
            hol_div=hol_div,
            is_open=is_open,
            session=session,
        )
        # An identical repeat is just pagination; a *conflicting* one means the
        # source disagrees with itself about whether the market was open, and
        # keeping either would make that a coin flip decided by page order.
        seen = days.get(trade_date)
        if seen is not None and seen != day:
            raise ValueError(
                f"J-Quants calendar reports conflicting holiday divisions for {trade_date.isoformat()}: "
                f"{seen.hol_div!r} and {hol_div!r}"
            )
        days[trade_date] = day
    return [days[key] for key in sorted(days)]
