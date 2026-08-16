from datetime import date

import pytest

from app.services.calendar_normalization import normalize_calendar


def test_half_day_session_is_an_open_trading_day():
    """HolDiv=2 (東証半日立会日) is a real trading day with real bars.

    Treating it as closed is what made 03 skip those dates entirely.
    """
    days = normalize_calendar([{"Date": "2026-01-05", "HolDiv": "2"}])

    assert [(day.trade_date, day.is_open, day.session) for day in days] == [
        (date(2026, 1, 5), True, "half_day")
    ]


@pytest.mark.parametrize(
    ("hol_div", "is_open", "session"),
    [
        # 0 = 非営業日
        ("0", False, None),
        # 1 = 営業日
        ("1", True, "full_day"),
        # 3 = 非営業日(祝日取引あり) — OSE derivatives trade, TSE cash equities
        # do not, and this system is TSE equities only.
        ("3", False, None),
    ],
)
def test_documented_holiday_divisions_map_to_open_state(hol_div, is_open, session):
    days = normalize_calendar([{"Date": "2026-01-05", "HolDiv": hol_div}])

    assert len(days) == 1
    assert (days[0].is_open, days[0].session) == (is_open, session)


def test_unknown_holiday_division_is_closed_and_keeps_the_raw_code():
    """A value J-Quants adds later must not crash the sync.

    Closed is the safe default — requesting bars for a date that isn't a
    trading day is worse than skipping it — and the raw code is kept so the
    unrecognised value stays visible in the data rather than being erased.
    """
    days = normalize_calendar([{"Date": "2026-01-05", "HolDiv": "4"}])

    assert len(days) == 1
    assert (days[0].is_open, days[0].session, days[0].hol_div) == (False, None, "4")


def test_result_is_ordered_by_date_and_identical_duplicates_collapse():
    """One row per date, chronologically — the calendar table is keyed by date,
    and pagination can legitimately repeat a row."""
    days = normalize_calendar(
        [
            {"Date": "2026-01-07", "HolDiv": "1"},
            {"Date": "2026-01-05", "HolDiv": "2"},
            {"Date": "2026-01-06", "HolDiv": "0"},
            {"Date": "2026-01-05", "HolDiv": "2"},
        ]
    )

    assert [day.trade_date for day in days] == [
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
    ]


def test_conflicting_duplicate_raises_instead_of_picking_one():
    """Same date, two different codes: whether the market was open would
    otherwise depend on page order. Failing the endpoint is the only honest
    answer — silently keeping one is a coin flip recorded as fact."""
    with pytest.raises(ValueError, match="2026-01-05"):
        normalize_calendar(
            [
                {"Date": "2026-01-05", "HolDiv": "1"},
                {"Date": "2026-01-05", "HolDiv": "0"},
            ]
        )
