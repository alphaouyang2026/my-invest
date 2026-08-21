"""Point-in-time stock pool: identity, liquidity, history and decision-day rules.

Driven through a real sync so the pool reads the same snapshot, roster and bar
versions production would hand it. The windows are shrunk (three days of
turnover, five of history) because the rules under test are about *which* days
count, not how many.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.models.market_data import (
    DataSnapshot,
    DataSnapshotHead,
    EndpointPublication,
    InstrumentMasterSnapshot,
    InstrumentMasterSnapshotMember,
    PublicationStatus,
)
from app.services.calendar_normalization import FULL_DAY, CalendarDay
from app.services.stock_pool import (
    AsOfNotTradingDayError,
    AsOfOutsideCoverageError,
    CalendarHistoryTooShortError,
    PoolExclusionReason,
    PoolWarningCode,
    StockPoolPolicy,
    build_stock_pool,
    policy_fingerprint,
    resolve_roster,
)
from app.api.stock_pool import get_stock_pool_policy
from app.main import app
from tests.fakes import FakeAdapter, InMemoryCalendarPort, full_bar_row

TRADING_DATES = [
    day
    for week in range(6)
    for day in (date(2025, 1, 6) + timedelta(days=7 * week + offset) for offset in range(5))
]
AS_OF = date(2025, 2, 14)
GAP_DAY = date(2025, 2, 11)
LATE_LISTING = date(2025, 2, 10)
DELISTED_AFTER = date(2025, 1, 17)

LIQUID = "13010"
THIN = "99990"
ETF = "13050"
PREFERRED = "25935"
FOREIGN = "17730"
WINDOW_GAP = "77770"
QUIET = "66660"
NO_BAR = "55550"
LATE = "44440"
BLANK = "33330"
PARTIAL = "22220"
DELISTED = "88880"

#: Comfortably clear of the 500M floor, and clear of it in the other direction
#: for THIN, so a test never turns on rounding.
RICH = 900_000_000
FLOOR_CLEARING = 600_000_000
BELOW_FLOOR = 100_000_000

TEST_POLICY = StockPoolPolicy(
    liquidity_window_days=3,
    required_history_days=5,
    required_bar_offsets=(5, 2),
)


def _master(as_of: date) -> list[dict]:
    rows = [
        {"Code": LIQUID, "Mkt": "0111", "ProdCat": "011"},
        {"Code": THIN, "Mkt": "0111", "ProdCat": "011"},
        # Sits on the "other" market tier, like every ETF and REIT in the real
        # feed, and carries a code ending in 0 exactly like common stock.
        {"Code": ETF, "Mkt": "0109", "ProdCat": "014"},
        # Prime, domestic, and still not investable: only the code suffix says so.
        {"Code": PREFERRED, "Mkt": "0111", "ProdCat": "011"},
        # Prime with a common-stock suffix: only ProdCat says so.
        {"Code": FOREIGN, "Mkt": "0111", "ProdCat": "021"},
        {"Code": WINDOW_GAP, "Mkt": "0111", "ProdCat": "011"},
        {"Code": QUIET, "Mkt": "0111", "ProdCat": "011"},
        {"Code": NO_BAR, "Mkt": "0111", "ProdCat": "011"},
        {"Code": LATE, "Mkt": "0111", "ProdCat": "011"},
        {"Code": BLANK, "Mkt": "0111", "ProdCat": "011"},
        {"Code": PARTIAL, "Mkt": "0111", "ProdCat": "011"},
    ]
    if as_of <= DELISTED_AFTER:
        rows.append({"Code": DELISTED, "Mkt": "0111", "ProdCat": "011"})
    return rows


def _bars(day: date) -> list[dict]:
    rows = [
        full_bar_row(LIQUID, day, turnover=FLOOR_CLEARING),
        full_bar_row(THIN, day, turnover=BELOW_FLOOR),
        full_bar_row(ETF, day, turnover=RICH),
        full_bar_row(PREFERRED, day, turnover=RICH),
        full_bar_row(FOREIGN, day, turnover=RICH),
    ]
    if day != GAP_DAY:
        rows.append(full_bar_row(WINDOW_GAP, day, turnover=RICH))
    rows.append(
        full_bar_row(QUIET, day, volume=0, turnover=0)
        if day == AS_OF
        else full_bar_row(QUIET, day, turnover=RICH)
    )
    if day != AS_OF:
        rows.append(full_bar_row(NO_BAR, day, turnover=RICH))
    if day >= LATE_LISTING:
        rows.append(full_bar_row(LATE, day, turnover=RICH))
    # What the real feed returns for a security that did not trade: a row with
    # every field null, not a row of zeroes.
    rows.append(
        {"Code": BLANK, "Date": day.isoformat()}
        if day == AS_OF
        else full_bar_row(BLANK, day, turnover=RICH)
    )
    partial = full_bar_row(PARTIAL, day, turnover=RICH)
    partial.pop("H")
    rows.append(partial)
    if day <= DELISTED_AFTER:
        rows.append(full_bar_row(DELISTED, day, turnover=RICH))
    return rows


@pytest.fixture
def calendar() -> InMemoryCalendarPort:
    """The independently written port from `fakes`, not the production one.

    Ticket 04 built it so a replaceable calendar could be shown to actually be
    replaceable; the pool is its first real consumer.
    """
    return InMemoryCalendarPort(
        [
            CalendarDay(trade_date=day, hol_div="1", is_open=True, session=FULL_DAY)
            for day in TRADING_DATES
        ]
    )


@pytest.fixture
def synced(make_workflow, db_session):
    adapter = FakeAdapter(trading_dates=TRADING_DATES, bars=_bars, master_rows=_master)
    workflow = make_workflow(adapter, batch_size=10)
    run_id = workflow.start().id
    workflow.execute(run_id)
    head = db_session.scalar(select(DataSnapshotHead).where(DataSnapshotHead.source == "jquants"))
    snapshot = db_session.get(DataSnapshot, head.snapshot_id)
    return adapter, snapshot


def _pool(db_session, snapshot, calendar, as_of=AS_OF, policy=TEST_POLICY):
    return build_stock_pool(
        db_session, snapshot, as_of=as_of, calendar=calendar, policy=policy
    )


def _symbols(items) -> set[str]:
    return {item.symbol for item in items}


def _reasons(pool, symbol) -> set[PoolExclusionReason]:
    return {
        reason
        for item in pool.exclusions
        if item.symbol == symbol
        for reason in item.reasons
    }


# --------------------------------------------------------------- roster backfill


def test_one_roster_is_fetched_per_week_plus_coverage_end(synced, db_session):
    adapter, _ = synced

    assert adapter.master_requested_dates == [
        date(2025, 1, 10),
        date(2025, 1, 17),
        date(2025, 1, 24),
        date(2025, 1, 31),
        date(2025, 2, 7),
        date(2025, 2, 14),
    ]
    stored = db_session.scalars(select(InstrumentMasterSnapshot.as_of_date)).all()
    assert sorted(stored) == adapter.master_requested_dates


def test_a_second_sync_refetches_no_roster_it_already_holds(synced, make_workflow, db_session):
    """The backfill's whole resume story: what is stored is the progress record."""
    _, _ = synced
    again = FakeAdapter(trading_dates=TRADING_DATES, bars=_bars, master_rows=_master)
    workflow = make_workflow(again, batch_size=10)
    run_id = workflow.start().id
    workflow.execute(run_id)

    assert again.master_requested_dates == []


def test_a_roster_without_product_categories_is_refetched(synced, make_workflow, db_session):
    """A roster stored before the product category existed cannot answer the
    identity question. Counting it as present would leave every pool built on
    it empty, with nothing to indicate why — so the backfill treats it as
    missing and fetches it again."""
    _, _ = synced
    stale = db_session.scalars(
        select(InstrumentMasterSnapshot).where(
            InstrumentMasterSnapshot.as_of_date == date(2025, 1, 24)
        )
    ).first()
    db_session.execute(
        update(InstrumentMasterSnapshotMember)
        .where(InstrumentMasterSnapshotMember.snapshot_id == stale.id)
        .values(product_category=None)
    )
    db_session.commit()

    again = FakeAdapter(trading_dates=TRADING_DATES, bars=_bars, master_rows=_master)
    workflow = make_workflow(again, batch_size=10)
    workflow.execute(workflow.start().id)

    assert again.master_requested_dates == [date(2025, 1, 24)]


def test_roster_membership_is_point_in_time(synced, db_session, calendar):
    """The security delisted in January is investable in January and gone in
    February — the fact today's roster alone cannot express."""
    _, snapshot = synced

    january = _pool(db_session, snapshot, calendar, as_of=DELISTED_AFTER)
    february = _pool(db_session, snapshot, calendar)

    assert DELISTED in _symbols(january.members)
    assert DELISTED not in _symbols(february.members)
    assert DELISTED not in _symbols(february.exclusions)
    assert january.master_as_of == DELISTED_AFTER
    assert february.master_as_of == AS_OF


def test_an_old_snapshot_keeps_its_pinned_roster_when_a_member_has_no_product_category(
    synced, db_session, calendar
):
    """NULL is a member-level identity failure, not a reason to replace the
    historical roster that an immutable snapshot points at."""
    _, snapshot = synced
    pinned = db_session.scalars(
        select(InstrumentMasterSnapshot).where(
            InstrumentMasterSnapshot.as_of_date == date(2025, 1, 24)
        )
    ).first()
    db_session.execute(
        update(InstrumentMasterSnapshotMember)
        .where(
            InstrumentMasterSnapshotMember.snapshot_id == pinned.id,
            InstrumentMasterSnapshotMember.symbol == LIQUID,
        )
        .values(product_category=None)
    )
    # This is the shape of a snapshot written before weekly roster cutoffs were
    # introduced: its one explicit master pointer is the roster visibility
    # boundary and must remain the roster that answers the query.
    snapshot.master_publish_sequence = None
    snapshot.master_snapshot_id = pinned.id
    db_session.commit()

    pool = _pool(db_session, snapshot, calendar, as_of=date(2025, 1, 24))

    assert pool.master_snapshot_id == pinned.id
    assert pool.master_as_of == date(2025, 1, 24)
    assert LIQUID not in _symbols(pool.members)
    assert LIQUID not in _symbols(pool.exclusions)
    assert pool.members, "other members with a known product category remain eligible"


def test_roster_resolution_never_looks_past_the_snapshot(synced, db_session):
    _, snapshot = synced
    roster, dated_after = resolve_roster(db_session, snapshot, AS_OF)

    assert roster.as_of_date == AS_OF
    assert dated_after is False


# ------------------------------------------------------------- identity filters


def test_non_prime_common_stock_is_absent_rather_than_listed_as_excluded(
    synced, db_session, calendar
):
    """ETF, preferred share and foreign listing each fail a different layer,
    and none of them belongs in an exclusion report a human reads."""
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    for symbol in (ETF, PREFERRED, FOREIGN):
        assert symbol not in _symbols(pool.members)
        assert symbol not in _symbols(pool.exclusions)


def test_widening_the_product_categories_admits_the_foreign_listing(
    synced, db_session, calendar
):
    """The 'can we add foreign stock later' answer, executed: one set literal."""
    _, snapshot = synced
    policy = StockPoolPolicy(
        liquidity_window_days=3,
        required_history_days=5,
        required_bar_offsets=(5, 2),
        allowed_product_categories=frozenset({"011", "021"}),
    )

    pool = _pool(db_session, snapshot, calendar, policy=policy)

    assert FOREIGN in _symbols(pool.members)


@pytest.mark.parametrize("field", ["prime_market_codes", "allowed_product_categories"])
def test_an_empty_identity_allowlist_admits_nothing(synced, db_session, calendar, field):
    _, snapshot = synced
    policy = replace(TEST_POLICY, **{field: frozenset()})

    pool = _pool(db_session, snapshot, calendar, policy=policy)

    assert pool.members == ()
    assert pool.exclusions == (), "identity failures do not belong in the exclusion report"


# ------------------------------------------------------------------- liquidity


def test_turnover_floor_admits_and_rejects(synced, db_session, calendar):
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    assert LIQUID in _symbols(pool.members)
    assert _reasons(pool, THIN) == {PoolExclusionReason.AVERAGE_TURNOVER_BELOW_FLOOR}


def test_average_turnover_is_reported_on_members(synced, db_session, calendar):
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    member = next(item for item in pool.members if item.symbol == LIQUID)
    assert member.average_turnover == Decimal(FLOOR_CLEARING)


def test_a_missing_bar_is_a_data_gap_not_a_quiet_day(synced, db_session, calendar):
    """The distinction the whole liquidity rule turns on: an absent row means we
    do not know, a present row with no turnover means nothing traded."""
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    assert PoolExclusionReason.MISSING_BARS_IN_LIQUIDITY_WINDOW in _reasons(pool, WINDOW_GAP)
    # Two of three days at 900M, the third missing, over a fixed denominator of
    # three: still above the floor, so the gap is the only complaint.
    assert PoolExclusionReason.AVERAGE_TURNOVER_BELOW_FLOOR not in _reasons(pool, WINDOW_GAP)


def test_the_denominator_is_the_window_not_the_days_with_data(synced, db_session, calendar):
    """A security halted for most of the window must not have its average
    flattered by dividing through the days it did trade."""
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    gap = next(item for item in pool.exclusions if item.symbol == WINDOW_GAP)
    assert gap.average_turnover == Decimal(RICH) * 2 / 3


def test_threshold_boundary_is_inclusive(synced, db_session, calendar):
    _, snapshot = synced
    policy = StockPoolPolicy(
        liquidity_window_days=3,
        required_history_days=5,
        required_bar_offsets=(5, 2),
        min_average_turnover_yen=Decimal(FLOOR_CLEARING),
    )

    pool = _pool(db_session, snapshot, calendar, policy=policy)

    assert LIQUID in _symbols(pool.members), "'not less than' must admit the exact value"


# ------------------------------------------------- history and the decision day


def test_a_late_listing_lacks_the_far_endpoint(synced, db_session, calendar):
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    assert _reasons(pool, LATE) == {PoolExclusionReason.INSUFFICIENT_PRICE_HISTORY}


def test_history_check_reads_only_the_endpoints(synced, db_session, calendar):
    """WINDOW_GAP is missing a day *between* the endpoints and keeps its
    history: requiring all 147 days would drop it for a day no return uses."""
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    assert PoolExclusionReason.INSUFFICIENT_PRICE_HISTORY not in _reasons(pool, WINDOW_GAP)


def test_no_bar_on_the_decision_day_is_excluded(synced, db_session, calendar):
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    assert PoolExclusionReason.NO_DECISION_DAY_BAR in _reasons(pool, NO_BAR)


def test_a_security_that_did_not_trade_on_the_decision_day_is_excluded(
    synced, db_session, calendar
):
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    assert _reasons(pool, QUIET) == {
        PoolExclusionReason.NO_TRADING_ACTIVITY_ON_DECISION_DAY
    }


def test_every_failed_rule_is_reported_not_just_the_first(synced, db_session, calendar):
    """An all-null decision day is both unpriceable and untraded. Reporting one
    reason would suggest fixing it would bring the security back."""
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    assert _reasons(pool, BLANK) == {
        PoolExclusionReason.UNTRADABLE_ON_DECISION_DAY,
        PoolExclusionReason.NO_TRADING_ACTIVITY_ON_DECISION_DAY,
    }


def test_a_missing_optional_field_does_not_cost_membership(synced, db_session, calendar):
    """`excluded` means a non-critical field is absent — here the high price.
    That says nothing about whether the security could be bought."""
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    assert PARTIAL in _symbols(pool.members)


# ----------------------------------------------------------- refusals and dates


def test_a_closed_day_is_refused_rather_than_rolled_back(synced, db_session, calendar):
    _, snapshot = synced
    # Inside coverage, so the refusal under test is "not a trading day" rather
    # than "outside coverage".
    saturday = date(2025, 2, 8)

    with pytest.raises(AsOfNotTradingDayError):
        _pool(db_session, snapshot, calendar, as_of=saturday)


def test_a_date_outside_coverage_is_refused_rather_than_answered_empty(
    synced, db_session, calendar
):
    _, snapshot = synced

    with pytest.raises(AsOfOutsideCoverageError):
        _pool(db_session, snapshot, calendar, as_of=date(2024, 6, 3))


def test_a_day_inside_bars_but_outside_the_calendar_is_this_module_s_error(
    synced, db_session, calendar
):
    """Production hits this: bars reach back to 2024-05-24 while the free
    calendar starts 2024-05-26, and the gap widens every week as the calendar
    window rolls forward and stored bars do not. Leaking the calendar's own
    exception would slip past every caller guarding on StockPoolError."""
    _, snapshot = synced
    snapshot.coverage_start = date(2025, 1, 1)
    db_session.commit()

    with pytest.raises(AsOfOutsideCoverageError):
        _pool(db_session, snapshot, calendar, as_of=date(2025, 1, 2))


def test_too_little_calendar_history_raises_instead_of_excluding_everyone(
    synced, db_session, calendar
):
    """Market-wide, so it is one error rather than 1,500 identical exclusions."""
    _, snapshot = synced

    with pytest.raises(CalendarHistoryTooShortError):
        _pool(db_session, snapshot, calendar, as_of=date(2025, 1, 8))


# ------------------------------------------------------------------- warnings


def test_the_delisting_warning_is_always_present(synced, db_session, calendar):
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar)

    codes = {warning.code for warning in pool.warnings}
    assert PoolWarningCode.FREE_DATA_NO_DELISTING_HISTORY in codes


def test_an_ordinary_midweek_query_raises_no_staleness_warning(synced, db_session, calendar):
    """Thursday against the previous Friday's roster is six days — normal, and a
    warning that fires here would be ignored everywhere."""
    _, snapshot = synced
    pool = _pool(db_session, snapshot, calendar, as_of=date(2025, 2, 13))

    assert pool.master_as_of == date(2025, 2, 7)
    codes = {warning.code for warning in pool.warnings}
    assert PoolWarningCode.MASTER_DATED_AFTER_AS_OF not in codes


def test_a_missing_week_of_roster_raises_the_staleness_warning(synced, db_session, calendar):
    _, snapshot = synced
    publication = db_session.scalar(
        select(EndpointPublication).where(
            EndpointPublication.endpoint == "equities/master",
            EndpointPublication.request_params["date"].astext == "2025-01-17",
        )
    )
    publication.status = PublicationStatus.FAILED
    # The schema refuses a sequence on anything unpublished, which is the point:
    # an unpublished generation is not market fact and cannot be ordered among
    # things that are.
    publication.publish_sequence = None
    db_session.commit()

    # Thursday: its own week's roster is the failed one, so resolution has to
    # reach back past a whole missing week.
    pool = _pool(db_session, snapshot, calendar, as_of=date(2025, 1, 23))

    assert pool.master_as_of == date(2025, 1, 10)
    warning = next(
        item for item in pool.warnings if item.code is PoolWarningCode.MASTER_DATED_AFTER_AS_OF
    )
    assert warning.detail["staleness_days"] == 13


def test_falling_forward_to_a_later_roster_warns(synced, db_session, calendar):
    """At the very start of coverage no roster exists yet. Using the earliest
    one beats refusing — the prices for those days are real — but it must say so."""
    _, snapshot = synced
    policy = StockPoolPolicy(
        liquidity_window_days=1, required_history_days=1, required_bar_offsets=(1,)
    )

    pool = _pool(db_session, snapshot, calendar, as_of=date(2025, 1, 8), policy=policy)

    assert pool.master_as_of == date(2025, 1, 10)
    warning = next(
        item for item in pool.warnings if item.code is PoolWarningCode.MASTER_DATED_AFTER_AS_OF
    )
    assert warning.detail["dated_after_as_of"] is True


# ------------------------------------------------------------------ fingerprint


def test_fingerprint_tracks_the_rules_not_the_data():
    tighter = StockPoolPolicy(min_average_turnover_yen=Decimal("1000000000"))

    assert policy_fingerprint(StockPoolPolicy()) == policy_fingerprint(StockPoolPolicy())
    assert policy_fingerprint(tighter) != policy_fingerprint(StockPoolPolicy())


def test_offsets_may_not_outreach_the_history_they_are_checked_against():
    with pytest.raises(ValueError):
        StockPoolPolicy(required_history_days=10, required_bar_offsets=(20,))


# ------------------------------------------------------------------------- api


@pytest.fixture
def pool_client(client):
    """The route under the shortened windows this fixture's six weeks of data
    can satisfy. The default 147-day history is a production baseline, not
    something a test corpus should have to reproduce."""
    app.dependency_overrides[get_stock_pool_policy] = lambda: TEST_POLICY
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_stock_pool_policy, None)


def test_endpoint_reports_members_exclusions_and_provenance(synced, pool_client):
    _, snapshot = synced

    response = pool_client.get(
        "/api/v1/stock-pool",
        params={"as_of": AS_OF.isoformat(), "snapshot_id": str(snapshot.id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["master_as_of"] == AS_OF.isoformat()
    assert body["snapshot_id"] == str(snapshot.id)
    assert body["policy_fingerprint"]
    assert {member["symbol"] for member in body["members"]}
    assert any(
        warning["code"] == PoolWarningCode.FREE_DATA_NO_DELISTING_HISTORY.value
        for warning in body["warnings"]
    )


def test_endpoint_refuses_a_closed_day(synced, pool_client):
    _, snapshot = synced

    response = pool_client.get(
        "/api/v1/stock-pool",
        params={"as_of": "2025-02-08", "snapshot_id": str(snapshot.id)},
    )

    assert response.status_code == 400
