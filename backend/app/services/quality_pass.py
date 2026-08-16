"""The contextual quality pass.

Rules here need something beyond one row — a calendar, a neighbouring date, or
the day's whole peer group — so they cannot be settled when a `BarVersion` is
inserted. They run once, after every batch has published and before the
snapshot is activated, and write aggregated findings rather than touching the
immutable version rows.

Two rules people expect to find here are deliberately absent. Duplicate
business keys and rows dated outside their request are caught while writing,
because the evidence is gone by the time a pass could look, and failing the
endpoint keeps the batch out of the database entirely. Volume outliers are not
a rule at all: an unusual volume is far more often an earnings day than a
defect, and a check that cries wolf teaches people to ignore findings.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.market_data import (
    BarQualityStatus,
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
class _Observed:
    """One bar as this run last saw it."""

    trade_date: date
    code: str
    raw_close: Decimal | None
    adjusted_close: Decimal | None
    raw_volume: Decimal | None
    adjustment_factor: Decimal | None
    quality_status: BarQualityStatus
    quality_rules: list[str]


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
    observed = _load_observed(session, run_id)
    findings: list[QualityFinding] = _calendar_revision_findings(
        session, run_id, source, calendar_publication_id, policy
    )

    if observed:
        by_date: dict[date, list[_Observed]] = defaultdict(list)
        for item in observed:
            by_date[item.trade_date].append(item)

        for trade_date in sorted(by_date):
            findings.extend(_evaluate_date(run_id, trade_date, by_date[trade_date], calendar, policy))
        findings.extend(_missing_bars_on_open_days(run_id, sorted(by_date), calendar))
        findings.extend(_adjustment_consistency(run_id, observed))

    for finding in findings:
        session.add(finding)

    return QualityOutcome(
        findings=len(findings),
        rejecting=sum(1 for item in findings if item.severity is QualitySeverity.REJECTING),
    )


def _load_observed(session: Session, run_id) -> list[_Observed]:
    """Every bar this run published, as of its own last observation."""
    rows = session.execute(
        select(BarRecord.trade_date, Instrument.source_code, BarVersion)
        .join(PublicationBarObservation, PublicationBarObservation.bar_record_id == BarRecord.id)
        .join(BarVersion, BarVersion.id == PublicationBarObservation.bar_version_id)
        .join(Instrument, Instrument.instrument_id == BarRecord.instrument_id)
        .join(
            EndpointPublication,
            EndpointPublication.id == PublicationBarObservation.publication_id,
        )
        .where(
            EndpointPublication.sync_run_id == run_id,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
        )
    ).all()

    return [
        _Observed(
            trade_date=trade_date,
            code=code or "",
            raw_close=version.raw_close,
            adjusted_close=version.adjusted_close,
            raw_volume=version.raw_volume,
            adjustment_factor=version.adjustment_factor,
            quality_status=version.quality_status,
            quality_rules=list(version.quality_rules or ()),
        )
        for trade_date, code, version in rows
    ]


def _finding(
    run_id,
    rule: QualityRule,
    trade_date: date,
    severity: QualitySeverity,
    affected: list[str],
    evaluated: int,
    policy: QualityPolicy,
) -> QualityFinding:
    return QualityFinding(
        sync_run_id=run_id,
        rule=rule.value,
        trade_date=trade_date,
        severity=severity,
        affected_count=len(affected),
        evaluated_count=evaluated,
        sample=sorted(affected)[: policy.finding_sample_limit],
    )


def _evaluate_date(
    run_id,
    trade_date: date,
    bars: list[_Observed],
    calendar: CalendarPort,
    policy: QualityPolicy,
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    evaluated = len(bars)

    # Cannot raise in practice: every date here came from this run's plan,
    # which is this calendar's own open days. Guarded anyway so a future caller
    # evaluating a wider set degrades to "not evaluated" rather than crashing.
    try:
        is_open = calendar.is_open(trade_date)
    except CalendarCoverageError:
        return findings

    if is_open:
        silent = [item.code for item in bars if item.raw_volume == 0]
        if silent:
            findings.append(
                _finding(
                    run_id,
                    QualityRule.NO_TRADING_ACTIVITY,
                    trade_date,
                    QualitySeverity.WARNING,
                    silent,
                    evaluated,
                    policy,
                )
            )

    negative = [item.code for item in bars if QualityRule.NEGATIVE_PRICE.value in item.quality_rules]
    if negative:
        ratio = len(negative) / evaluated if evaluated else 0.0
        # A single impossible price is that security's problem; a whole day of
        # them is a broken feed, and no amount of per-security marking makes
        # the day usable.
        severity = (
            QualitySeverity.REJECTING
            if ratio > policy.negative_price_escalation_ratio
            else QualitySeverity.WARNING
        )
        findings.append(
            _finding(
                run_id,
                QualityRule.NEGATIVE_PRICE,
                trade_date,
                severity,
                negative,
                evaluated,
                policy,
            )
        )

    return findings


def _missing_bars_on_open_days(run_id, covered: list[date], calendar: CalendarPort) -> list[QualityFinding]:
    """Open days inside this run's own range that produced no bars at all."""
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


def _calendar_revision_findings(
    session: Session,
    run_id,
    source: str,
    current_publication_id,
    policy: QualityPolicy,
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
        codes = _codes_with_bars(session, source, day)
        if codes:
            findings.append(
                _finding(
                    run_id,
                    QualityRule.CALENDAR_DISAGREEMENT,
                    day,
                    QualitySeverity.WARNING,
                    codes,
                    len(codes),
                    policy,
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


def _codes_with_bars(session: Session, source: str, trade_date: date) -> list[str]:
    return list(
        session.scalars(
            select(Instrument.source_code)
            .join(BarRecord, BarRecord.instrument_id == Instrument.instrument_id)
            .where(BarRecord.source == source, BarRecord.trade_date == trade_date)
        ).all()
    )


def _adjustment_consistency(run_id, observed: list[_Observed]) -> list[QualityFinding]:
    """The adjusted series must only step where the factor says it does.

    Deliberately formula-agnostic: it asserts that `adjusted/raw` holds steady
    between consecutive observed dates unless an adjustment factor other than 1
    appears, which is true of any multiplicative convention. The exact algebra
    J-Quants uses is not confirmed, so this stays a warning and is kept out of
    the escalation arithmetic until real data pins the formula down.
    """
    by_code: dict[str, list[_Observed]] = defaultdict(list)
    for item in observed:
        by_code[item.code].append(item)

    findings: list[QualityFinding] = []
    for code, items in by_code.items():
        items.sort(key=lambda entry: entry.trade_date)
        for earlier, later in zip(items, items[1:]):
            previous = _ratio(earlier)
            current = _ratio(later)
            if previous is None or current is None or previous == current:
                continue
            factor = later.adjustment_factor
            if factor is not None and factor != 1:
                continue
            findings.append(
                QualityFinding(
                    sync_run_id=run_id,
                    rule=QualityRule.ADJUSTMENT_INCONSISTENT.value,
                    trade_date=later.trade_date,
                    severity=QualitySeverity.WARNING,
                    affected_count=1,
                    evaluated_count=1,
                    sample=[code],
                )
            )
    return findings


def _ratio(item: _Observed) -> Decimal | None:
    if item.raw_close in (None, 0) or item.adjusted_close is None:
        return None
    return item.adjusted_close / item.raw_close
