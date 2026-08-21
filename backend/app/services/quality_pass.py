"""The contextual quality pass.

Rules here need something beyond one row — a calendar, a neighbouring date, or
the day's whole peer group — so they cannot be settled when a `BarVersion` is
inserted. They run once per *evaluation* and write aggregated findings rather
than touching the immutable version rows.

An evaluation names the population it judged. A sync judges what that run
published, before its snapshot exists; a re-validation judges everything the
head snapshot resolves to, with today's rules. `Scope` is the one thing that
differs between them — every rule below is written against it, so neither
producer has a copy of the other's logic.

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

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.models.market_data import (
    BarRecord,
    BarVersion,
    DataSnapshot,
    EndpointPublication,
    Instrument,
    PublicationBarObservation,
    PublicationStatus,
    QualityFinding,
    QualitySeverity,
    TradingCalendar,
)
from app.services import snapshot_reader
from app.services.calendar_port import CalendarCoverageError, CalendarPort
from app.services.quality_rules import QualityPolicy, QualityRule


@dataclass(frozen=True)
class RunScope:
    """The bars one sync run published.

    Narrower than the snapshot that run goes on to create: a sync re-checks a
    window, and judging the untouched years behind it would re-report the same
    findings on every incremental.
    """

    run_id: uuid.UUID


@dataclass(frozen=True)
class SnapshotScope:
    """Everything a snapshot resolves to.

    What a re-validation judges, because "does today's rule set still accept
    the data we hold" is a question about the whole readable history, not about
    whichever window was fetched last.
    """

    source: str
    bar_publish_sequence: int


Scope = RunScope | SnapshotScope


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


def evaluate(
    session: Session,
    *,
    evaluation_id: uuid.UUID,
    scope: Scope,
    source: str,
    calendar: CalendarPort,
    calendar_publication_id,
    policy: QualityPolicy,
) -> QualityOutcome:
    """Write this evaluation's contextual findings and report whether any reject."""
    findings = [
        *_calendar_revision_findings(
            session, evaluation_id, source, calendar_publication_id, policy
        ),
        *_per_date_findings(session, evaluation_id, scope, calendar, policy),
        *_missing_trading_days(session, evaluation_id, scope, calendar),
        *_adjustment_findings(session, evaluation_id, scope, policy),
    ]

    for finding in findings:
        session.add(finding)

    return QualityOutcome(
        findings=len(findings),
        rejecting=sum(1 for item in findings if item.severity is QualitySeverity.REJECTING),
    )


def _observations(scope: Scope) -> Select:
    """The bars this evaluation judges, as a join every rule starts from.

    Both branches expose the same entities — `BarRecord`, `BarVersion`, and a
    joinable `instrument_id` — so a rule never has to know which scope it is
    running under.
    """
    if isinstance(scope, RunScope):
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
                EndpointPublication.sync_run_id == scope.run_id,
                EndpointPublication.status == PublicationStatus.PUBLISHED,
            )
        )

    # Resolved through snapshot_reader rather than by re-deriving "highest
    # published sequence at or below the cutoff" here: that rule has exactly
    # one home (§9.1), and a second copy of it would be free to drift.
    members = snapshot_reader.snapshot_member_query(
        scope.source, scope.bar_publish_sequence
    ).subquery()
    return (
        select(BarRecord.trade_date)
        .select_from(members)
        .join(BarRecord, BarRecord.id == members.c.bar_record_id)
        .join(BarVersion, BarVersion.id == members.c.bar_version_id)
    )


def _per_date_findings(
    session: Session,
    evaluation_id: uuid.UUID,
    scope: Scope,
    calendar: CalendarPort,
    policy: QualityPolicy,
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
        _observations(scope)
        .add_columns(
            func.count().label("evaluated"),
            func.count().filter(negative).label("negative"),
            func.count().filter(silent).label("silent"),
        )
        .group_by(BarRecord.trade_date)
    ).all()

    flagged_negative = [row.trade_date for row in counts if row.negative]
    flagged_silent = [row.trade_date for row in counts if row.silent]
    negative_samples = _samples(session, scope, negative, flagged_negative, policy)
    silent_samples = _samples(session, scope, silent, flagged_silent, policy)

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
                    evaluation_id, QualityRule.NEGATIVE_PRICE, row.trade_date, severity,
                    row.negative, row.evaluated, negative_samples.get(row.trade_date, []),
                )
            )
        if row.silent and _is_open(calendar, row.trade_date):
            findings.append(
                _finding(
                    evaluation_id, QualityRule.NO_TRADING_ACTIVITY, row.trade_date,
                    QualitySeverity.WARNING,
                    row.silent, row.evaluated, silent_samples.get(row.trade_date, []),
                )
            )
    return findings


def _samples(
    session: Session, scope: Scope, condition, dates: list[date], policy: QualityPolicy
) -> dict[date, list[str]]:
    """A capped worked example per flagged date, in one pass.

    ROW_NUMBER rather than a query per date: a rule that fires on most days
    would otherwise mean hundreds of round trips.
    """
    if not dates:
        return {}
    ranked = (
        _observations(scope)
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


def _missing_trading_days(
    session: Session, evaluation_id: uuid.UUID, scope: Scope, calendar: CalendarPort
) -> list[QualityFinding]:
    """Open days inside this evaluation's own range that produced no bars at all."""
    covered = sorted(session.scalars(_observations(scope).distinct()).all())
    if not covered:
        return []
    try:
        expected = calendar.open_days_between(covered[0], covered[-1])
    except CalendarCoverageError:
        return []

    present = set(covered)
    return [
        QualityFinding(
            evaluation_id=evaluation_id,
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


def _adjustment_findings(
    session: Session, evaluation_id: uuid.UUID, scope: Scope, policy: QualityPolicy
) -> list[QualityFinding]:
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
        _observations(scope)
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
            evaluation_id, QualityRule.ADJUSTMENT_INCONSISTENT, row.trade_date,
            QualitySeverity.WARNING, row.affected, row.affected,
            sorted(row.codes)[: policy.finding_sample_limit],
        )
        for row in rows
    ]


def _calendar_revision_findings(
    session: Session,
    evaluation_id: uuid.UUID,
    source: str,
    current_publication_id,
    policy: QualityPolicy,
) -> list[QualityFinding]:
    """Days this calendar closed that we already hold bars for.

    Within a single publication the calendar and the bars cannot disagree —
    every date fetched came from that calendar's own open days. The
    disagreement only becomes possible across a revision, when the annual
    republication closes a day the stored data already covers. Diffing against
    the calendar in force before this one is therefore both the cheapest and
    the only meaningful check: the diff is usually empty, and a full re-scan
    would re-report the same days on every sync.

    Scope-independent on purpose. A re-validation judges the head snapshot's
    own calendar, so "the one before" is the last snapshot built on a
    *different* calendar — which is the same pair the sync that produced the
    head compared, and therefore the same verdict rather than a silently
    emptied one.
    """
    previous_publication_id = _previous_calendar_publication(
        session, source, current_publication_id
    )
    if previous_publication_id is None:
        return []

    before = _calendar_map(session, previous_publication_id)
    after = _calendar_map(session, current_publication_id)

    findings = []
    for day, was_open in before.items():
        if not was_open or after.get(day) is not False:
            continue
        codes = _codes_with_bars(session, source, day, policy)
        if codes:
            findings.append(
                _finding(
                    evaluation_id, QualityRule.CALENDAR_DISAGREEMENT, day,
                    QualitySeverity.WARNING, len(codes), len(codes), codes,
                )
            )
    return findings


def _previous_calendar_publication(session: Session, source: str, current_publication_id):
    """The most recent snapshot calendar that differs from the one being judged.

    Ordered by version rather than read off the head pointer so it holds for a
    re-validation too: the head *is* the snapshot under evaluation there, and
    the `!=` is what steps past it without needing to know that.
    """
    return session.scalar(
        select(DataSnapshot.calendar_publication_id)
        .where(
            DataSnapshot.source == source,
            DataSnapshot.calendar_publication_id != current_publication_id,
        )
        .order_by(DataSnapshot.version.desc())
        .limit(1)
    )


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
    # Cannot raise for a sync: every date there came from the run's plan, which
    # is this calendar's own open days. A re-validation reaches wider — a
    # snapshot can hold dates outside the calendar it was built with — so this
    # degrades to "not evaluated" rather than crashing.
    try:
        return calendar.is_open(trade_date)
    except CalendarCoverageError:
        return False


def _finding(
    evaluation_id: uuid.UUID,
    rule: QualityRule,
    trade_date: date,
    severity: QualitySeverity,
    affected: int,
    evaluated: int,
    sample: list[str],
) -> QualityFinding:
    return QualityFinding(
        evaluation_id=evaluation_id,
        rule=rule.value,
        trade_date=trade_date,
        severity=severity,
        affected_count=affected,
        evaluated_count=evaluated,
        sample=list(sample),
    )
