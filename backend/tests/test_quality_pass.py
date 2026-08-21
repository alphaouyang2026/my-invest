"""The contextual quality pass and the verdict it produces.

The pass runs after every batch has published and before the snapshot is
activated, so what it decides is what a backtest is later allowed to use.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.market_data import (
    DataSnapshot,
    DataSnapshotHead,
    QualityEvaluation,
    QualityFinding,
    QualitySeverity,
)
from app.services.quality_rules import QualityPolicy, QualityRule
from tests.fakes import FakeAdapter, bar_row

# Mon-Fri open, the following Sat closed.
OPEN_DAYS = [date(2024, 3, 4) + timedelta(days=offset) for offset in range(5)]
SATURDAY = date(2024, 3, 9)


def _findings(session, run_id) -> dict[str, QualityFinding]:
    """This run's findings, reached through the evaluation that wrote them."""
    rows = session.scalars(
        select(QualityFinding)
        .join(QualityEvaluation, QualityEvaluation.id == QualityFinding.evaluation_id)
        .where(QualityEvaluation.sync_run_id == run_id)
    ).all()
    return {row.rule: row for row in rows}


def _snapshot(session, run_id) -> DataSnapshot:
    return session.scalar(select(DataSnapshot).where(DataSnapshot.sync_run_id == run_id))


def _run(make_workflow, adapter, **kwargs):
    workflow = make_workflow(adapter, **kwargs)
    run_id = workflow.start().id
    workflow.execute(run_id)
    return run_id


def test_one_impossible_price_is_a_warning_and_leaves_the_snapshot_usable(
    make_workflow, session_factory
):
    """A single bad security is that security's problem. Marking it untradable
    is the whole remedy; throwing away the day would be worse than the defect."""

    def bars(trade_date: date) -> list[dict]:
        healthy = [bar_row(f"1{index:04d}", trade_date) for index in range(200)]
        return [*healthy, bar_row("99990", trade_date, close=-5)]

    run_id = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bars))

    with session_factory() as session:
        finding = _findings(session, run_id)[QualityRule.NEGATIVE_PRICE.value]
        assert finding.severity is QualitySeverity.WARNING
        assert finding.affected_count == 1
        assert finding.evaluated_count == 201
        assert finding.sample == ["99990"]
        assert _snapshot(session, run_id).is_backtest_eligible is True


def test_a_day_full_of_impossible_prices_makes_the_snapshot_unusable(
    make_workflow, session_factory
):
    """No amount of per-security marking makes a broken feed usable."""

    def bars(trade_date: date) -> list[dict]:
        return [bar_row("13010", trade_date, close=-5), bar_row("13020", trade_date)]

    run_id = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bars))

    with session_factory() as session:
        finding = _findings(session, run_id)[QualityRule.NEGATIVE_PRICE.value]
        assert finding.severity is QualitySeverity.REJECTING
        assert _snapshot(session, run_id).is_backtest_eligible is False


def test_the_escalation_threshold_is_configurable(make_workflow, session_factory):
    """Same data, a looser policy, and the day is merely flagged."""

    def bars(trade_date: date) -> list[dict]:
        return [bar_row("13010", trade_date, close=-5), bar_row("13020", trade_date)]

    run_id = _run(
        make_workflow,
        FakeAdapter(trading_dates=OPEN_DAYS, bars=bars),
        quality_policy=QualityPolicy(negative_price_escalation_ratio=0.9),
    )

    with session_factory() as session:
        assert (
            _findings(session, run_id)[QualityRule.NEGATIVE_PRICE.value].severity
            is QualitySeverity.WARNING
        )
        assert _snapshot(session, run_id).is_backtest_eligible is True


def test_a_day_with_no_trades_is_only_flagged(make_workflow, session_factory):
    """A suspension is a real market state, so it is recorded and nothing more.

    J-Quants reports a no-sale day as Null across price and volume alike, not
    as a zero — two years of real data contain 92,772 all-null bars and no
    zero-volume ones at all.
    """

    def bars(trade_date: date) -> list[dict]:
        row = bar_row("13010", trade_date)
        row["Vo"] = None
        return [row]

    run_id = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bars))

    with session_factory() as session:
        finding = _findings(session, run_id)[QualityRule.NO_TRADING_ACTIVITY.value]
        assert finding.severity is QualitySeverity.WARNING
        assert _snapshot(session, run_id).is_backtest_eligible is True


def test_warnings_never_accumulate_into_a_rejection(make_workflow, session_factory):
    """The severe/warning line was drawn deliberately; volume of warnings must
    not quietly redraw it."""

    def bars(trade_date: date) -> list[dict]:
        rows = []
        for index in range(20):
            row = bar_row(f"1{index:04d}", trade_date)
            row["Vo"] = None
            rows.append(row)
        return rows

    run_id = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bars))

    with session_factory() as session:
        findings = _findings(session, run_id)
        assert findings
        assert all(item.severity is QualitySeverity.WARNING for item in findings.values())
        assert _snapshot(session, run_id).is_backtest_eligible is True


def test_a_broken_evaluator_fails_the_task_instead_of_guessing(
    make_workflow, session_factory, monkeypatch
):
    """A crashed checker produced no verdict — not "fine", not "bad". Recording
    ineligible would be indistinguishable from having actually checked."""
    import app.services.quality_pass as quality_pass

    def explode(*args, **kwargs):
        raise RuntimeError("evaluator bug")

    monkeypatch.setattr(quality_pass, "evaluate", explode)

    workflow = make_workflow(FakeAdapter(trading_dates=OPEN_DAYS))
    run_id = workflow.start().id

    with pytest.raises(RuntimeError):
        workflow.execute(run_id)

    with session_factory() as session:
        assert _snapshot(session, run_id) is None
        assert session.get(DataSnapshotHead, "jquants") is None


def test_an_open_day_that_returned_nothing_is_recorded(make_workflow, session_factory):
    """A trading day with no bars at all is a hole, not an empty day. Nothing
    on the row-local side can notice it — there is no row to judge."""
    silent_day = OPEN_DAYS[2]

    def bars(trade_date: date) -> list[dict]:
        return [] if trade_date == silent_day else [bar_row("13010", trade_date)]

    run_id = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bars))

    with session_factory() as session:
        finding = _findings(session, run_id)[QualityRule.MISSING_TRADING_DAY.value]
        assert finding.trade_date == silent_day
        assert finding.severity is QualitySeverity.WARNING


def test_an_unexplained_step_in_the_adjusted_series_is_flagged(make_workflow, session_factory):
    """The adjusted series may only move relative to raw where the factor says
    it does. Deliberately formula-agnostic: any multiplicative convention makes
    the ratio hold steady while the factor is 1."""

    def bars(trade_date: date) -> list[dict]:
        row = bar_row("13010", trade_date)
        row["AdjFactor"] = 1
        # The adjusted price silently halves on the third day with no factor
        # to explain it — far beyond the 0.1 yen rounding the source applies.
        row["AdjC"] = 5 if trade_date >= OPEN_DAYS[2] else 10
        return [row]

    run_id = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bars))

    with session_factory() as session:
        finding = _findings(session, run_id)[QualityRule.ADJUSTMENT_INCONSISTENT.value]
        assert finding.trade_date == OPEN_DAYS[2]
        assert finding.severity is QualitySeverity.WARNING
        assert _snapshot(session, run_id).is_backtest_eligible is True


def test_a_split_explained_by_its_factor_is_not_flagged(make_workflow, session_factory):
    """A real corporate action must not be reported as a defect."""

    def bars(trade_date: date) -> list[dict]:
        row = bar_row("13010", trade_date)
        if trade_date >= OPEN_DAYS[2]:
            row["AdjC"] = 5
            row["AdjFactor"] = 0.5
        else:
            row["AdjC"] = 10
            row["AdjFactor"] = 1
        return [row]

    run_id = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bars))

    with session_factory() as session:
        assert QualityRule.ADJUSTMENT_INCONSISTENT.value not in _findings(session, run_id)


def test_rounding_in_the_adjusted_series_is_not_a_defect(make_workflow, session_factory):
    """The source rounds the adjusted price to 0.1 yen, so a split-affected
    security's ratio drifts every day. Comparing ratios exactly reported 46,756
    steps across 485 days of real data against roughly 500 real actions."""

    def bars(trade_date: date) -> list[dict]:
        # A 3:1 split: adjusted is raw/3 rounded to 0.1, so the ratio wobbles.
        closes = {0: 3995, 1: 4000, 2: 3990, 3: 4010, 4: 3985}
        close = closes[OPEN_DAYS.index(trade_date)]
        row = bar_row("13010", trade_date, close=close)
        row["AdjFactor"] = 1
        row["AdjC"] = round(close / 3, 1)
        return [row]

    run_id = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bars))

    with session_factory() as session:
        assert QualityRule.ADJUSTMENT_INCONSISTENT.value not in _findings(session, run_id)


def test_an_empty_incremental_inherits_the_previous_verdict(make_workflow, session_factory):
    """Nothing new was published, so the new snapshot resolves to byte-identical
    bar versions. Re-judging could only produce the same answer, and declaring
    it unknown would make a clean dataset unusable for no reason."""

    def bad(trade_date: date) -> list[dict]:
        return [bar_row("13010", trade_date, close=-5), bar_row("13020", trade_date)]

    first = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=bad))
    second = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=lambda d: []))

    with session_factory() as session:
        assert _snapshot(session, first).is_backtest_eligible is False
        inherited = _snapshot(session, second)
        assert inherited.is_backtest_eligible is False
        assert not _findings(session, second), "an empty incremental re-runs nothing"


def test_a_revision_that_closes_a_day_we_hold_bars_for_is_flagged(
    make_workflow, session_factory
):
    """The only way the calendar and the bars can ever disagree.

    Within one run they cannot: every date fetched came from that run's own
    calendar. It takes a republication closing a day the stored data already
    covers — which is exactly what the annual March update can do.
    """
    closed_later = OPEN_DAYS[3]
    _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))

    still_open = [day for day in OPEN_DAYS if day != closed_later]
    second = _run(
        make_workflow,
        FakeAdapter(trading_dates=still_open, closed_dates=[closed_later]),
    )

    with session_factory() as session:
        finding = _findings(session, second)[QualityRule.CALENDAR_DISAGREEMENT.value]
        assert finding.trade_date == closed_later
        assert finding.severity is QualitySeverity.WARNING
        assert finding.sample == ["13010"]


def test_an_unchanged_calendar_reports_no_disagreement(make_workflow, session_factory):
    """The diff is empty on an ordinary sync, so the rule stays silent instead
    of re-reporting the same days forever."""
    _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))
    second = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))

    with session_factory() as session:
        assert QualityRule.CALENDAR_DISAGREEMENT.value not in _findings(session, second)


def test_snapshot_versions_count_up_per_source(make_workflow, session_factory):
    """A number shown to a person has to count; gaps read as lost data."""
    first = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))
    second = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))

    with session_factory() as session:
        assert _snapshot(session, first).version == 1
        assert _snapshot(session, second).version == 2
