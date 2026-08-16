"""The contextual quality pass.

Rules here need something beyond one row — a calendar, a neighbouring date, or
the day's whole peer group — so they cannot be settled when a `BarVersion` is
inserted. They run once, after every batch has published and before the
snapshot is activated, and write aggregated findings rather than touching the
immutable version rows.

Every rule aggregates in SQL. The first version of this module loaded each
observed bar into Python and counted there, which is fine for the thousand rows
a test builds and fatal for the two million a real initial import produces: the
worker was killed by the kernel three times before hitting its attempt cap. A
finding is an aggregate over (rule, trade date), so the database is where it
should be computed; only the aggregates and a capped sample ever cross into
Python.

Two rules people expect to find here are deliberately absent. Duplicate
business keys and rows dated outside their request are caught while writing,
because the evidence is gone by the time a pass could look, and failing the
endpoint keeps the batch out of the database entirely. Volume outliers are not
a rule at all: an unusual volume is far more often an earnings day than a
defect, and a check that cries wolf teaches people to ignore findings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.models.market_data import (
    BarRecord,
    BarVersion,
    DataSnapshot,
    DataSnapshotHead,
    EndpointPublication,
    Instrument,
    PublicationBarObservation,
    PublicationStatus,
    QualityFinding,
    QualitySeverity,
    TradingCalendar,
)
from app.services.calendar_port import CalendarCoverageError, CalendarPort
from app.services.quality_rules import QualityPolicy, QualityRule


@dataclass(frozen=True)
class QualityOutcome:
    findings: int
    rejecting: int

    @property
    def is_backtest_eligible(self) -> bool:
        """Warnings never make a snapshot unusable.

        The severe/warning line was drawn deliberately; letting warnings
        accumulate into a rejection would quietly redraw it.
        """
        return self.rejecting == 0


def evaluate_run(
    session: Session,
    run_id,
    source: str,
    calendar: CalendarPort,
    calendar_publication_id,
    policy: QualityPolicy,
) -> QualityOutcome:
    """Write this run's contextual findings and report whether any reject."""
    findings = [
        *_calendar_revision_findings(session, run_id, source, calendar_publication_id, policy),
        *_per_date_findings(session, run_id, calendar, policy),
        *_missing_trading_days(session, run_id, calendar),
        *_adjustment_findings(session, run_id, policy),
    ]

    for finding in findings:
        session.add(finding)

    return QualityOutcome(
        findings=len(findings),
        rejecting=sum(1 for item in findings if item.severity is QualitySeverity.REJECTING),
    )


def _observations(run_id) -> Select:
    """This run's published bars, as a join every rule starts from."""
    return (
        select(BarRecord.trade_date)
        .select_from(PublicationBarObservation)
        .join(
            EndpointPublication,
            EndpointPublication.id == PublicationBarObservation.publication_id,
        )
        .join(BarRecord, BarRecord.id == PublicationBarObservation.bar_record_id)
        .join(BarVersion, BarVersion.id == PublicationBarObservation.bar_version_id)
        .where(
            EndpointPublication.sync_run_id == run_id,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
        )
    )


def _per_date_findings(
    session: Session, run_id, calendar: CalendarPort, policy: QualityPolicy
) -> list[QualityFinding]:
    """Impossible prices and silent securities, counted per trade date."""
    negative = BarVersion.quality_rules.any(QualityRule.NEGATIVE_PRICE.value)
    # A no-sale day arrives as NULL, not zero — J-Quants records price and
    # volume alike as Null when nothing traded, and in two years of real data
    # there is not one zero-volume row against 92,772 all-null ones. Checking
    # for zero left this rule silent in production while the days it exists to
    # surface were the single largest category in the data.
    silent = BarVersion.raw_volume.is_(None)

    counts = session.execute(
        _observations(run_id)
        .add_columns(
            func.count().label("evaluated"),
            func.count().filter(negative).label("negative"),
            func.count().filter(silent).label("silent"),
        )
        .group_by(BarRecord.trade_date)
    ).all()

    flagged_negative = [row.trade_date for row in counts if row.negative]
    flagged_silent = [row.trade_date for row in counts if row.silent]
    negative_samples = _samples(session, run_id, negative, flagged_negative, policy)
    silent_samples = _samples(session, run_id, silent, flagged_silent, policy)

    findings: list[QualityFinding] = []
    for row in counts:
        if row.negative:
            ratio = row.negative / row.evaluated if row.evaluated else 0.0
            # One impossible price is that security's problem. A whole day of
            # them is a broken feed, and no amount of per-security marking
            # makes the day usable.
            severity = (
                QualitySeverity.REJECTING
                if ratio > policy.negative_price_escalation_ratio
                else QualitySeverity.WARNING
            )
            findings.append(
                _finding(
                    run_id, QualityRule.NEGATIVE_PRICE, row.trade_date, severity,
                    row.negative, row.evaluated, negative_samples.get(row.trade_date, []),
                )
            )
        if row.silent and _is_open(calendar, row.trade_date):
            findings.append(
                _finding(
                    run_id, QualityRule.NO_TRADING_ACTIVITY, row.trade_date,
                    QualitySeverity.WARNING,
                    row.silent, row.evaluated, silent_samples.get(row.trade_date, []),
                )
            )
    return findings


def _samples(
    session: Session, run_id, condition, dates: list[date], policy: QualityPolicy
) -> dict[date, list[str]]:
    """A capped worked example per flagged date, in one pass.

    ROW_NUMBER rather than a query per date: a rule that fires on most days
    would otherwise mean hundreds of round trips.
    """
    if not dates:
        return {}
    ranked = (
        _observations(run_id)
        .add_columns(
            Instrument.source_code,
            func.row_number()
            .over(partition_by=BarRecord.trade_date, order_by=Instrument.source_code)
            .label("rank"),
        )
        .join(Instrument, Instrument.instrument_id == BarRecord.instrument_id)
        .where(and_(condition, BarRecord.trade_date.in_(dates)))
        .subquery()
    )
    rows = session.execute(
        select(ranked.c.trade_date, ranked.c.source_code).where(
            ranked.c.rank <= policy.finding_sample_limit
        )
    ).all()

    samples: dict[date, list[str]] = {}
    for trade_date, code in rows:
        samples.setdefault(trade_date, []).append(code)
    return samples


def _missing_trading_days(session: Session, run_id, calendar: CalendarPort) -> list[QualityFinding]:
    """Open days inside this run's own range that produced no bars at all."""
    covered = sorted(
        session.scalars(_observations(run_id).distinct()).all()
    )
    if not covered:
        return []
    try:
        expected = calendar.open_days_between(covered[0], covered[-1])
    except CalendarCoverageError:
        return []

    present = set(covered)
    return [
        QualityFinding(
            sync_run_id=run_id,
            rule=QualityRule.MISSING_TRADING_DAY.value,
            trade_date=day,
            severity=QualitySeverity.WARNING,
            affected_count=0,
            evaluated_count=0,
            sample=[],
        )
        for day in expected
        if day not in present
    ]


def _adjustment_findings(session: Session, run_id, policy: QualityPolicy) -> list[QualityFinding]:
    """The adjusted series must only step where the factor says it does.

    Real data settled the formula this once guessed at: `adjusted_close` is
    `raw_close` times the cumulative factor, **rounded to 0.1 yen**. That
    rounding makes the ratio drift in its fifth decimal every single day for
    any split-affected security, so an exact comparison flagged 46,756 steps
    across 485 days against roughly 500 genuine corporate actions.

    The comparison is therefore against the adjusted price the previous ratio
    implies, within the rounding quantum, rather than against the ratio itself.
    It stays a warning and out of the escalation arithmetic.

    The LAG runs in the database; only the violations are returned.
    """
    ratio = BarVersion.adjusted_close / func.nullif(BarVersion.raw_close, 0)
    ratios = (
        _observations(run_id)
        .add_columns(
            Instrument.source_code,
            BarVersion.raw_close.label("raw_close"),
            BarVersion.adjusted_close.label("adjusted_close"),
            BarVersion.adjustment_factor.label("factor"),
            func.lag(ratio)
            .over(partition_by=BarRecord.instrument_id, order_by=BarRecord.trade_date)
            .label("previous"),
        )
        .join(Instrument, Instrument.instrument_id == BarRecord.instrument_id)
        .subquery()
    )

    implied = ratios.c.raw_close * ratios.c.previous
    rows = session.execute(
        select(
            ratios.c.trade_date,
            func.count().label("affected"),
            func.array_agg(func.distinct(ratios.c.source_code)).label("codes"),
        )
        .where(
            ratios.c.previous.is_not(None),
            ratios.c.adjusted_close.is_not(None),
            func.abs(ratios.c.adjusted_close - implied) > policy.adjustment_tolerance_yen,
            (ratios.c.factor.is_(None)) | (ratios.c.factor == 1),
        )
        .group_by(ratios.c.trade_date)
    ).all()

    return [
        _finding(
            run_id, QualityRule.ADJUSTMENT_INCONSISTENT, row.trade_date,
            QualitySeverity.WARNING, row.affected, row.affected,
            sorted(row.codes)[: policy.finding_sample_limit],
        )
        for row in rows
    ]


def _calendar_revision_findings(
    session: Session, run_id, source: str, current_publication_id, policy: QualityPolicy
) -> list[QualityFinding]:
    """Days this calendar closed that we already hold bars for.

    Within a single run the calendar and the bars cannot disagree — every date
    fetched came from this calendar's own open days. The disagreement only
    becomes possible across a revision, when the annual republication closes a
    day the stored data already covers. Diffing against the previous snapshot's
    calendar is therefore both the cheapest and the only meaningful check: the
    diff is usually empty, and a full re-scan would re-report the same days on
    every sync.
    """
    head = session.get(DataSnapshotHead, source)
    if head is None:
        return []
    previous = session.get(DataSnapshot, head.snapshot_id)
    if previous is None or previous.calendar_publication_id == current_publication_id:
        return []

    before = _calendar_map(session, previous.calendar_publication_id)
    after = _calendar_map(session, current_publication_id)

    findings = []
    for day, was_open in before.items():
        if not was_open or after.get(day) is not False:
            continue
        codes = _codes_with_bars(session, source, day, policy)
        if codes:
            findings.append(
                _finding(
                    run_id, QualityRule.CALENDAR_DISAGREEMENT, day,
                    QualitySeverity.WARNING, len(codes), len(codes), codes,
                )
            )
    return findings


def _calendar_map(session: Session, publication_id) -> dict[date, bool]:
    return dict(
        session.execute(
            select(TradingCalendar.trade_date, TradingCalendar.is_open).where(
                TradingCalendar.publication_id == publication_id
            )
        ).all()
    )


def _codes_with_bars(session: Session, source: str, trade_date: date, policy: QualityPolicy) -> list[str]:
    return list(
        session.scalars(
            select(Instrument.source_code)
            .join(BarRecord, BarRecord.instrument_id == Instrument.instrument_id)
            .where(BarRecord.source == source, BarRecord.trade_date == trade_date)
            .order_by(Instrument.source_code)
            .limit(policy.finding_sample_limit)
        ).all()
    )


def _is_open(calendar: CalendarPort, trade_date: date) -> bool:
    # Cannot raise in practice: every date here came from this run's plan,
    # which is this calendar's own open days. Guarded anyway so a future caller
    # evaluating a wider set degrades to "not evaluated" rather than crashing.
    try:
        return calendar.is_open(trade_date)
    except CalendarCoverageError:
        return False


def _finding(
    run_id,
    rule: QualityRule,
    trade_date: date,
    severity: QualitySeverity,
    affected: int,
    evaluated: int,
    sample: list[str],
) -> QualityFinding:
    return QualityFinding(
        sync_run_id=run_id,
        rule=rule.value,
        trade_date=trade_date,
        severity=severity,
        affected_count=affected,
        evaluated_count=evaluated,
        sample=list(sample),
    )
