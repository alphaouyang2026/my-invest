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
    QualityFinding,
    QualitySeverity,
)
from app.services.quality_rules import QualityPolicy, QualityRule
from tests.fakes import FakeAdapter, bar_row

# Mon-Fri open, the following Sat closed.
OPEN_DAYS = [date(2024, 3, 4) + timedelta(days=offset) for offset in range(5)]
SATURDAY = date(2024, 3, 9)


def _findings(session, run_id) -> dict[str, QualityFinding]:
    rows = session.scalars(
        select(QualityFinding).where(QualityFinding.sync_run_id == run_id)
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
    """A suspension is a real market state, so it is recorded and nothing more."""

    def bars(trade_date: date) -> list[dict]:
        row = bar_row("13010", trade_date)
        row["Vo"] = 0
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
            row["Vo"] = 0
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

    monkeypatch.setattr(quality_pass, "evaluate_run", explode)

    workflow = make_workflow(FakeAdapter(trading_dates=OPEN_DAYS))
    run_id = workflow.start().id

    with pytest.raises(RuntimeError):
        workflow.execute(run_id)

    with session_factory() as session:
        assert _snapshot(session, run_id) is None
        assert session.get(DataSnapshotHead, "jquants") is None


def test_snapshot_versions_count_up_per_source(make_workflow, session_factory):
    """A number shown to a person has to count; gaps read as lost data."""
    first = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))
    second = _run(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))

    with session_factory() as session:
        assert _snapshot(session, first).version == 1
        assert _snapshot(session, second).version == 2
