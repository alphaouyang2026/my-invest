"""The investable universe as it stood on a past decision day.

Three questions have to be answered together, and answering any of them with
today's facts is what makes a backtest lie:

1. *Was this a security we invest in?* Prime Market, domestic, common stock —
   judged against the roster **of that date**, never today's. Over two years of
   real data, today's roster is missing 88 securities that were delisted and 28
   that were demoted, while carrying 35 that had not yet been promoted; the
   first two are survivorship bias, the third is look-ahead.
2. *Could it actually be bought?* A momentum ranking is happy to pick the
   thinnest security on the exchange. The turnover floor is what keeps the
   simulated fills inside the range a real order could have been filled at.
3. *Can a signal even be computed for it?* Securities whose history is too
   short belong out of the pool, not silently scored as zero in ticket 06.

Everything here is a pure function of `(snapshot, as_of, policy)`, so nothing
is persisted: the result can always be recomputed, and a stored copy could
only ever drift from the facts it was derived from.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.orm import Session

from app.models.market_data import (
    BarQualityStatus,
    BarVersion,
    DataSnapshot,
    EndpointPublication,
    InstrumentMasterSnapshot,
    InstrumentMasterSnapshotMember,
    PublicationStatus,
)
from app.core.logging import get_logger
from app.services.calendar_port import CalendarCoverageError, CalendarPort
from app.services.snapshot_reader import snapshot_member_query

logger = get_logger(__name__)

#: How stale the roster may be before it is worth saying so, in calendar days.
#: Not a policy knob: it is a direct consequence of fetching one roster per
#: week. A mid-week decision day legitimately sits up to six days after its
#: week's roster; anything past seven means a week is missing. Making it
#: configurable would let it drift out of step with the fetch cadence, and a
#: warning that fires on every ordinary query is a warning nobody reads.
MASTER_STALENESS_TOLERANCE_DAYS = 7

#: Raised, not reported per security: when the calendar cannot reach back far
#: enough, every security fails identically, and 1,500 identical exclusions
#: would bury the one fact that matters — that the data source, not the market,
#: is the limit here.
CALENDAR_COVERAGE_SHORT = "calendar_coverage_short"


class StockPoolError(RuntimeError):
    """The pool cannot be built as asked."""


class AsOfOutsideCoverageError(StockPoolError):
    """The decision day lies outside what the snapshot can answer for.

    Deliberately not an empty pool: "no security qualified" and "we hold no
    data for this day" look identical in a result set, and only one of them is
    worth investigating.
    """


class AsOfNotTradingDayError(StockPoolError):
    """The decision day is not an open trading day.

    The pool refuses to quietly roll the date back. Snapping to the previous
    open day is one call for the caller to make deliberately; doing it here
    would answer a question nobody asked, exactly when a wrong date is what
    needs to be noticed.
    """


class CalendarHistoryTooShortError(StockPoolError):
    reason = CALENDAR_COVERAGE_SHORT


class RosterUnavailableError(StockPoolError):
    """No instrument roster is visible to this snapshot at all."""


class PoolExclusionReason(str, Enum):
    """Why a security that *is* Prime common stock still did not make the pool.

    Identity failures (wrong market, ETF, preferred share) are not here: they
    are reported by absence, because listing all ~2,900 of them on every query
    would bury the few dozen near misses that are worth reading.
    """

    #: The 20-day window has open trading days for which no bar exists at all.
    #: Distinct from a bar that exists and shows no trading — that one is a
    #: real, informative zero.
    MISSING_BARS_IN_LIQUIDITY_WINDOW = "missing_bars_in_liquidity_window"
    AVERAGE_TURNOVER_BELOW_FLOOR = "average_turnover_below_floor"
    #: This security's own history is too short or unusable at the offsets a
    #: signal needs — as opposed to the calendar itself being too short, which
    #: is market-wide and raises instead.
    INSUFFICIENT_PRICE_HISTORY = "insufficient_price_history"
    NO_DECISION_DAY_BAR = "no_decision_day_bar"
    UNTRADABLE_ON_DECISION_DAY = "untradable_on_decision_day"
    NO_TRADING_ACTIVITY_ON_DECISION_DAY = "no_trading_activity_on_decision_day"


class PoolWarningCode(str, Enum):
    #: Always on for this feed. The roster has no listing-status field at all,
    #: so a security's absence can never be told apart from a gap in what we
    #: hold. Weekly rosters shrink this to the securities delisted before our
    #: earliest roster; they cannot remove it.
    FREE_DATA_NO_DELISTING_HISTORY = "free_data_no_delisting_history"
    #: The roster used sits further from the decision day than one weekly
    #: step, in either direction.
    MASTER_DATED_AFTER_AS_OF = "master_dated_after_as_of"


@dataclass(frozen=True)
class StockPoolPolicy:
    """Every eligibility rule in one injectable place.

    A frozen dataclass rather than `Settings`, matching `SyncPolicy` and
    `QualityPolicy`: a test varies a threshold without environment variables or
    a restart, and ticket 14 can later feed it from stored configuration
    without any of these call sites changing.
    """

    #: Market-tier allow-list. An allow-list rather than a deny-list on
    #: purpose: when the exchange invents a tier, the safe default is "not
    #: investable" — a deny-list defaults the other way.
    prime_market_codes: frozenset[str] = frozenset({"0111"})
    #: The source's `ProdCat`. `011` is domestic stock; this is what separates
    #: a company from an ETF (`014`), a REIT (`013`) or a foreign listing
    #: (`021`). Neither the market tier nor the code suffix can do it: ETFs and
    #: REITs carry codes ending in 0 just like common stock, and there is a
    #: foreign issuer listed on Prime. Widening this set is all that admitting
    #: foreign shares would take *here* — the work would be downstream, in
    #: withholding tax and corporate actions.
    allowed_product_categories: frozenset[str] = frozenset({"011"})
    #: JPX five-digit codes end in 0 for common stock; preferred and class
    #: shares end in something else. This is the only signal that separates
    #: them — they share both the market tier and the product category with
    #: the common shares of the same issuer.
    require_common_stock_suffix: bool = True
    common_stock_suffix: str = "0"

    liquidity_window_days: int = 20
    min_average_turnover_yen: Decimal = Decimal("500000000")

    #: Open trading days of history a security must have behind it. Stated as a
    #: neutral count, not as "126 + 21": the pool has no business knowing that
    #: its caller runs a momentum strategy, and the moment a second strategy
    #: exists, a momentum-shaped constant here would be wrong. Ticket 06
    #: injects the value its own parameters imply; 147 is the baseline used
    #: when nobody says otherwise.
    required_history_days: int = 147
    #: Offsets, in open trading days back from the decision day, whose bars
    #: must be usable. Only the endpoints a return is computed between are
    #: checked — requiring all 147 days would drop a security for a single
    #: halted day that no calculation reads.
    required_bar_offsets: tuple[int, ...] = (147, 21)

    def __post_init__(self) -> None:
        if self.liquidity_window_days < 1:
            raise ValueError("liquidity_window_days must be positive")
        if self.required_bar_offsets and max(self.required_bar_offsets) > self.required_history_days:
            raise ValueError("required_bar_offsets reach further back than required_history_days")


DEFAULT_POLICY = StockPoolPolicy()


@dataclass(frozen=True)
class PoolMember:
    instrument_id: uuid.UUID
    symbol: str
    #: Carried rather than recomputed: both the signal stage and the backtest
    #: want it, and it is already in hand from the query that admitted the
    #: security.
    average_turnover: Decimal


@dataclass(frozen=True)
class PoolExclusion:
    instrument_id: uuid.UUID
    symbol: str
    #: Every rule the security failed, not just the first. Reporting one reason
    #: invites the conclusion that relaxing it would bring the security back,
    #: which is false whenever a second rule also failed.
    reasons: tuple[PoolExclusionReason, ...]
    average_turnover: Decimal | None


@dataclass(frozen=True)
class PoolWarning:
    code: PoolWarningCode
    detail: dict


@dataclass(frozen=True)
class StockPool:
    as_of: date
    snapshot_id: uuid.UUID
    master_snapshot_id: uuid.UUID
    master_as_of: date
    members: tuple[PoolMember, ...]
    exclusions: tuple[PoolExclusion, ...]
    warnings: tuple[PoolWarning, ...]
    #: Identifies the rules this result was produced under. Two runs whose
    #: membership differs are otherwise ambiguous between "the data changed"
    #: and "the thresholds changed".
    policy_fingerprint: str


@dataclass(frozen=True)
class TurnoverSummary:
    symbol: str
    bars_present: int
    total_turnover: Decimal


@dataclass(frozen=True)
class EligibilitySummary:
    history_priceable: bool
    decision_bar_present: bool
    decision_priceable: bool
    decision_has_activity: bool


def policy_fingerprint(policy: StockPoolPolicy) -> str:
    """Hash of the rules, written field by field.

    Not `asdict`: the frozensets and the `Decimal` have no canonical JSON form,
    and letting the serialiser choose one would make the fingerprint depend on
    which library version produced it.
    """
    payload = {
        "prime_market_codes": sorted(policy.prime_market_codes),
        "allowed_product_categories": sorted(policy.allowed_product_categories),
        "require_common_stock_suffix": policy.require_common_stock_suffix,
        "common_stock_suffix": policy.common_stock_suffix,
        "liquidity_window_days": policy.liquidity_window_days,
        "min_average_turnover_yen": str(policy.min_average_turnover_yen),
        "required_history_days": policy.required_history_days,
        "required_bar_offsets": list(policy.required_bar_offsets),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def resolve_roster(
    session: Session, snapshot: DataSnapshot, as_of: date
) -> tuple[InstrumentMasterSnapshot, bool]:
    """The roster this snapshot would have seen on `as_of`.

    Visibility runs through the same publish sequence the bars use, so a roster
    fetched after the snapshot was cut cannot leak into it — the property that
    makes a finished backtest reproducible.

    Returns the roster and whether it had to be dated after `as_of`. Falling
    forward happens only at the very start of coverage, and is preferred to
    refusing: the price data for those first weeks exists, and a roster a few
    days late misstates a handful of securities, where an error would cost the
    whole opening stretch of every backtest.
    """
    cutoff = _roster_cutoff(session, snapshot)
    visible = (
        select(InstrumentMasterSnapshot)
        .join(
            EndpointPublication,
            EndpointPublication.id == InstrumentMasterSnapshot.publication_id,
        )
        .where(
            InstrumentMasterSnapshot.source == snapshot.source,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
            EndpointPublication.publish_sequence.is_not(None),
            EndpointPublication.publish_sequence <= cutoff,
        )
    )
    at_or_before = session.scalars(
        visible.where(InstrumentMasterSnapshot.as_of_date <= as_of)
        .order_by(
            InstrumentMasterSnapshot.as_of_date.desc(),
            EndpointPublication.publish_sequence.desc(),
        )
        .limit(1)
    ).first()
    if at_or_before is not None:
        return at_or_before, False

    earliest = session.scalars(
        visible.order_by(
            InstrumentMasterSnapshot.as_of_date,
            EndpointPublication.publish_sequence,
        ).limit(1)
    ).first()
    if earliest is None:
        raise RosterUnavailableError(
            f"No instrument roster is visible to snapshot {snapshot.id}"
        )
    return earliest, True


def _roster_cutoff(session: Session, snapshot: DataSnapshot) -> int:
    """The roster publish sequence this snapshot is frozen at.

    Not `bar_publish_sequence`: rosters are published after the bars they
    accompany, so every one of them sits above that cutoff and comparing
    against it would hide all of them. Snapshots written before rosters became
    plural carry no cutoff, and for them the roster they point at is the answer.
    """
    if snapshot.master_publish_sequence is not None:
        return snapshot.master_publish_sequence
    return session.scalar(
        select(EndpointPublication.publish_sequence)
        .join(
            InstrumentMasterSnapshot,
            InstrumentMasterSnapshot.publication_id == EndpointPublication.id,
        )
        .where(InstrumentMasterSnapshot.id == snapshot.master_snapshot_id)
    )


def _eligible_members(roster_id: uuid.UUID, policy: StockPoolPolicy) -> Select:
    """Roster rows that pass all three identity layers.

    Evaluated in SQL so the ~2,900 securities that fail identity never leave
    the database.
    """
    conditions = [
        InstrumentMasterSnapshotMember.snapshot_id == roster_id,
        # These are allow-lists, including when empty: no configured market or
        # product category means no security is eligible. Omitting an empty
        # predicate would invert that safe meaning into "allow everything".
        InstrumentMasterSnapshotMember.market_code.in_(
            sorted(policy.prime_market_codes)
        ),
        # NULL fails `IN` and that is the intended reading: a roster row
        # ingested before the product category was stored says nothing about
        # what kind of security it is, and guessing on the permissive side is
        # how an ETF ends up in an equity pool.
        InstrumentMasterSnapshotMember.product_category.in_(
            sorted(policy.allowed_product_categories)
        ),
    ]
    if policy.require_common_stock_suffix:
        conditions.append(
            InstrumentMasterSnapshotMember.symbol.like(f"%{policy.common_stock_suffix}")
        )
    return select(
        InstrumentMasterSnapshotMember.instrument_id.label("instrument_id"),
        InstrumentMasterSnapshotMember.symbol.label("symbol"),
    ).where(*conditions)


def _turnover_by_instrument(
    session: Session,
    snapshot: DataSnapshot,
    eligible: Select,
    window: list[date],
) -> dict[uuid.UUID, TurnoverSummary]:
    """Bars present and total turnover per security, aggregated in the database.

    One decision day is ~1,500 securities x 20 days; a backtest walks a hundred
    decision days. Pulling those rows into Python is the shape of failure the
    quality pass already hit in production, so the arithmetic stays in SQL and
    only one row per security comes back.

    Two queries rather than one outer join. Securities with no bars at all have
    to appear in the result, and expressing that as a LEFT JOIN onto a
    `DISTINCT ON` subquery hands the planner something it can get badly wrong:
    on a freshly loaded table with no statistics it chose to re-run the inner
    scan per security and took over twenty minutes. Listing the eligible
    securities separately and filling the gaps here costs one small query and
    leaves only an inner join, which cannot degrade that way.
    """
    totals: dict[uuid.UUID, TurnoverSummary] = {
        row.instrument_id: TurnoverSummary(
            symbol=row.symbol,
            bars_present=0,
            total_turnover=Decimal(0),
        )
        for row in session.execute(eligible).all()
    }
    members = snapshot_member_query(
        snapshot.source,
        snapshot.bar_publish_sequence,
        trade_dates=window,
        instrument_ids=list(totals),
    ).subquery()
    rows = session.execute(
        select(
            members.c.instrument_id,
            func.count().label("bars_present"),
            # A bar that exists with no turnover counts as a genuine zero: the
            # security was listed and nothing traded. Only a missing bar is a
            # data gap, and that is what `bars_present` reports.
            func.coalesce(func.sum(func.coalesce(BarVersion.trading_value, 0)), 0).label(
                "total_turnover"
            ),
        )
        .select_from(members)
        .join(BarVersion, BarVersion.id == members.c.bar_version_id)
        .group_by(members.c.instrument_id)
    ).all()
    for row in rows:
        totals[row.instrument_id] = TurnoverSummary(
            symbol=totals[row.instrument_id].symbol,
            bars_present=row.bars_present,
            total_turnover=Decimal(row.total_turnover),
        )
    return totals


def _eligibility_by_instrument(
    session: Session,
    snapshot: DataSnapshot,
    instrument_ids: list[uuid.UUID],
    history_dates: set[date],
    decision_date: date,
) -> dict[uuid.UUID, EligibilitySummary]:
    """Aggregate every endpoint verdict into one row per security.

    Missing securities are filled in Python rather than expressed as a LEFT
    JOIN onto the snapshot-resolution subquery. The turnover query uses the
    same shape: keeping the resolution on the inner side prevents PostgreSQL
    from choosing a per-security rescan that is catastrophic at production
    scale, while the grouped result still returns at most one row per security.
    """
    summaries = {
        instrument_id: EligibilitySummary(
            history_priceable=not history_dates,
            decision_bar_present=False,
            decision_priceable=False,
            decision_has_activity=False,
        )
        for instrument_id in instrument_ids
    }
    checked_dates = sorted({*history_dates, decision_date})
    members = snapshot_member_query(
        snapshot.source,
        snapshot.bar_publish_sequence,
        trade_dates=checked_dates,
        instrument_ids=instrument_ids,
    ).subquery()
    history_priceable = case(
        (
            and_(
                members.c.trade_date.in_(sorted(history_dates)),
                BarVersion.adjusted_close.is_not(None),
                BarVersion.quality_status != BarQualityStatus.UNTRADABLE,
            ),
            1,
        ),
        else_=0,
    )
    decision_bar_present = case(
        (members.c.trade_date == decision_date, 1),
        else_=0,
    )
    decision_priceable = case(
        (
            and_(
                members.c.trade_date == decision_date,
                BarVersion.adjusted_close.is_not(None),
                BarVersion.quality_status != BarQualityStatus.UNTRADABLE,
            ),
            1,
        ),
        else_=0,
    )
    decision_has_activity = case(
        (
            and_(
                members.c.trade_date == decision_date,
                BarVersion.raw_volume.is_not(None),
                BarVersion.raw_volume > 0,
            ),
            1,
        ),
        else_=0,
    )
    rows = session.execute(
        select(
            members.c.instrument_id,
            func.sum(history_priceable).label("history_priceable_count"),
            func.sum(decision_bar_present).label("decision_bar_count"),
            func.sum(decision_priceable).label("decision_priceable_count"),
            func.sum(decision_has_activity).label("decision_activity_count"),
        )
        .select_from(members)
        .join(BarVersion, BarVersion.id == members.c.bar_version_id)
        .group_by(members.c.instrument_id)
    ).all()
    for row in rows:
        summaries[row.instrument_id] = EligibilitySummary(
            history_priceable=row.history_priceable_count == len(history_dates),
            decision_bar_present=row.decision_bar_count > 0,
            decision_priceable=row.decision_priceable_count > 0,
            decision_has_activity=row.decision_activity_count > 0,
        )
    return summaries


def build_stock_pool(
    session: Session,
    snapshot: DataSnapshot,
    *,
    as_of: date,
    calendar: CalendarPort,
    policy: StockPoolPolicy = DEFAULT_POLICY,
) -> StockPool:
    """Which securities were investable on `as_of`, as this snapshot sees it.

    `calendar` is passed in rather than constructed here so it stays bound to
    the snapshot's own calendar publication: a port that followed the latest
    calendar would let next March's revision move the windows underneath a
    backtest that finished last year.
    """
    if not (snapshot.coverage_start <= as_of <= snapshot.coverage_end):
        raise AsOfOutsideCoverageError(
            f"{as_of.isoformat()} is outside snapshot coverage "
            f"({snapshot.coverage_start} .. {snapshot.coverage_end})"
        )
    # Bars and calendar do not cover the same span, and the gap grows: the free
    # calendar window rolls forward while bars already stored never shrink, so
    # the oldest days of coverage permanently lose their calendar. Surfaced as
    # this module's own error, because a caller guarding on `StockPoolError`
    # would otherwise miss it entirely.
    try:
        open_day = calendar.is_open(as_of)
    except CalendarCoverageError as exc:
        raise AsOfOutsideCoverageError(
            f"{as_of.isoformat()} lies within the snapshot's bars but outside its "
            f"calendar: {exc}"
        ) from exc
    if not open_day:
        raise AsOfNotTradingDayError(f"{as_of.isoformat()} is not an open trading day")

    # Half-open windows throughout: the decision is made after the close, but
    # letting the decision day's own price into the lookback it is compared
    # against is how a backtest reads its own answer key.
    try:
        history = calendar.window_back(as_of, policy.required_history_days)
    except CalendarCoverageError as exc:
        raise CalendarHistoryTooShortError(
            f"The calendar reaches back fewer than {policy.required_history_days} "
            f"open days before {as_of.isoformat()}: {exc}"
        ) from exc
    liquidity_window = calendar.window_back(as_of, policy.liquidity_window_days)
    offset_dates = {offset: history[len(history) - offset] for offset in policy.required_bar_offsets}

    roster, dated_after = resolve_roster(session, snapshot, as_of)
    eligible = _eligible_members(roster.id, policy)

    turnover = _turnover_by_instrument(session, snapshot, eligible, liquidity_window)
    eligibility = _eligibility_by_instrument(
        session,
        snapshot,
        list(turnover),
        set(offset_dates.values()),
        as_of,
    )

    members: list[PoolMember] = []
    exclusions: list[PoolExclusion] = []
    for instrument_id, summary in turnover.items():
        reasons: list[PoolExclusionReason] = []
        # Fixed denominator. Dividing by the days that happened to have data
        # would flatter a security halted for most of the window — exactly the
        # security the floor exists to keep out.
        average = summary.total_turnover / policy.liquidity_window_days
        if summary.bars_present < policy.liquidity_window_days:
            reasons.append(PoolExclusionReason.MISSING_BARS_IN_LIQUIDITY_WINDOW)
        if average < policy.min_average_turnover_yen:
            reasons.append(PoolExclusionReason.AVERAGE_TURNOVER_BELOW_FLOOR)

        instrument_eligibility = eligibility[instrument_id]
        if not instrument_eligibility.history_priceable:
            reasons.append(PoolExclusionReason.INSUFFICIENT_PRICE_HISTORY)

        if not instrument_eligibility.decision_bar_present:
            reasons.append(PoolExclusionReason.NO_DECISION_DAY_BAR)
        else:
            if not instrument_eligibility.decision_priceable:
                reasons.append(PoolExclusionReason.UNTRADABLE_ON_DECISION_DAY)
            # A security that traded nothing on the decision day has no price a
            # next-open fill could honestly be modelled from. `excluded` is
            # deliberately not checked: it means a non-critical field is
            # missing, which says nothing about whether the security could be
            # bought.
            if not instrument_eligibility.decision_has_activity:
                reasons.append(PoolExclusionReason.NO_TRADING_ACTIVITY_ON_DECISION_DAY)

        if reasons:
            exclusions.append(
                PoolExclusion(
                    instrument_id=instrument_id,
                    symbol=summary.symbol,
                    reasons=tuple(reasons),
                    average_turnover=average,
                )
            )
        else:
            members.append(
                PoolMember(
                    instrument_id=instrument_id,
                    symbol=summary.symbol,
                    average_turnover=average,
                )
            )

    pool = StockPool(
        as_of=as_of,
        snapshot_id=snapshot.id,
        master_snapshot_id=roster.id,
        master_as_of=roster.as_of_date,
        members=tuple(sorted(members, key=lambda item: item.symbol)),
        exclusions=tuple(sorted(exclusions, key=lambda item: item.symbol)),
        warnings=_warnings(as_of, roster.as_of_date, dated_after),
        policy_fingerprint=policy_fingerprint(policy),
    )
    logger.debug(
        "stock_pool.built",
        as_of=as_of.isoformat(),
        master_as_of=roster.as_of_date.isoformat(),
        members=len(pool.members),
        exclusions=len(pool.exclusions),
        warnings=list(pool.warnings),
    )
    return pool


def _warnings(as_of: date, master_as_of: date, dated_after: bool) -> tuple[PoolWarning, ...]:
    warnings = [
        PoolWarning(
            code=PoolWarningCode.FREE_DATA_NO_DELISTING_HISTORY,
            detail={
                "reason": (
                    "The instrument roster carries no listing-status field, so a "
                    "security absent from it cannot be distinguished from one we "
                    "never fetched."
                )
            },
        )
    ]
    staleness = (as_of - master_as_of).days
    if dated_after or abs(staleness) > MASTER_STALENESS_TOLERANCE_DAYS:
        warnings.append(
            PoolWarning(
                code=PoolWarningCode.MASTER_DATED_AFTER_AS_OF,
                detail={
                    "as_of": as_of.isoformat(),
                    "master_as_of": master_as_of.isoformat(),
                    # Positive: the roster predates the decision day by more
                    # than one weekly step, so a week is missing. Negative: no
                    # roster existed that early and a later one was used.
                    "staleness_days": staleness,
                    "dated_after_as_of": dated_after,
                },
            )
        )
    return tuple(warnings)
