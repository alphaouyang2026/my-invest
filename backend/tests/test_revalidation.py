"""Re-validation: the same bars, judged again by today's rules.

The point of the feature is that a rule change costs one background pass rather
than a fourteen-hour re-fetch, and that the old verdict survives it — a backtest
already bound to a snapshot must not have its answer rewritten underneath it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.market_data import (
    DataSnapshot,
    DataSnapshotHead,
    QualityEvaluation,
    QualityEvaluationKind,
    QualityEvaluationStatus,
    QualityFinding,
    QualitySeverity,
)
from app.services.jquants_sync_workflow import SyncConflict
from app.services.quality_revalidation import RevalidationConflict, RevalidationUnavailable
from app.services.quality_rules import QualityPolicy, QualityRule
from tests.fakes import FakeAdapter, bar_row

OPEN_DAYS = [date(2024, 3, 4) + timedelta(days=offset) for offset in range(5)]


def _one_bad_price(trade_date: date) -> list[dict]:
    """One impossible price against 99 good ones — a warning by default, and a
    rejection once the escalation threshold drops below 1%."""
    healthy = [bar_row(f"1{index:04d}", trade_date) for index in range(99)]
    return [*healthy, bar_row("99990", trade_date, close=-5)]


def _sync(make_workflow, adapter, **kwargs):
    workflow = make_workflow(adapter, **kwargs)
    run_id = workflow.start().id
    workflow.execute(run_id)
    return run_id


def _revalidate(workflow):
    evaluation_id = workflow.start().id
    workflow.execute(evaluation_id)
    return evaluation_id


def _snapshots(session) -> list[DataSnapshot]:
    return list(session.scalars(select(DataSnapshot).order_by(DataSnapshot.version)).all())


def _findings(session, evaluation_id) -> dict[str, QualityFinding]:
    rows = session.scalars(
        select(QualityFinding).where(QualityFinding.evaluation_id == evaluation_id)
    ).all()
    return {row.rule: row for row in rows}


def test_revalidating_produces_a_new_snapshot_over_the_same_data(
    make_workflow, revalidation_workflow, session_factory
):
    """Same cutoff, same master, next version, head moved forward.

    Not a step backwards: coverage is identical, only the judgement is newer.
    """
    _sync(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))

    _revalidate(revalidation_workflow)

    with session_factory() as session:
        first, second = _snapshots(session)
        head = session.get(DataSnapshotHead, "jquants")

    assert second.version == first.version + 1
    assert second.bar_publish_sequence == first.bar_publish_sequence
    assert second.master_snapshot_id == first.master_snapshot_id
    assert second.calendar_publication_id == first.calendar_publication_id
    assert (second.coverage_start, second.coverage_end) == (first.coverage_start, first.coverage_end)
    assert head.snapshot_id == second.id
    # Nothing was asked of the source, so nothing may claim to have been.
    assert second.sync_run_id is None
    assert second.mode is None
    assert second.verified_start is None and second.verified_end is None


def test_a_stricter_rule_rejects_the_data_without_refetching_it(
    make_workflow, make_revalidation, session_factory
):
    """The reason this exists: change the threshold, re-judge, keep the bars."""
    adapter = FakeAdapter(trading_dates=OPEN_DAYS, bars=_one_bad_price)
    _sync(make_workflow, adapter)
    requests_after_sync = len(adapter.bar_requests)

    strict = make_revalidation(quality_policy=QualityPolicy(negative_price_escalation_ratio=0.0))
    evaluation_id = _revalidate(strict)

    with session_factory() as session:
        first, second = _snapshots(session)
        finding = _findings(session, evaluation_id)[QualityRule.NEGATIVE_PRICE.value]

    assert first.is_backtest_eligible is True, "the original verdict must not be rewritten"
    assert second.is_backtest_eligible is False
    assert finding.severity is QualitySeverity.REJECTING
    assert len(adapter.bar_requests) == requests_after_sync, "re-validation fetches nothing"


def test_the_earlier_snapshots_findings_are_left_exactly_as_they_were(
    make_workflow, make_revalidation, session_factory
):
    """Both verdicts stay readable, which is what makes a rule change reviewable."""
    _sync(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS, bars=_one_bad_price))

    with session_factory() as session:
        sync_evaluation = session.scalar(
            select(QualityEvaluation).where(
                QualityEvaluation.kind == QualityEvaluationKind.SYNC
            )
        )
        before = _findings(session, sync_evaluation.id)[QualityRule.NEGATIVE_PRICE.value]
        before_severity, before_id = before.severity, before.id

    _revalidate(make_revalidation(quality_policy=QualityPolicy(negative_price_escalation_ratio=0.0)))

    with session_factory() as session:
        after = _findings(session, sync_evaluation.id)[QualityRule.NEGATIVE_PRICE.value]

    assert after.id == before_id
    assert after.severity is before_severity is QualitySeverity.WARNING


def test_the_same_conclusion_still_produces_a_snapshot(
    make_workflow, revalidation_workflow, session_factory
):
    """Deciding two evaluations "agree" needs a definition of agreement that
    lies at some boundary. The cost of not deciding is one row."""
    _sync(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))

    _revalidate(revalidation_workflow)
    _revalidate(revalidation_workflow)

    with session_factory() as session:
        versions = [item.version for item in _snapshots(session)]
        verdicts = {item.is_backtest_eligible for item in _snapshots(session)}

    assert versions == [1, 2, 3]
    assert verdicts == {True}


def test_the_evaluation_records_the_thresholds_it_ran_under(
    make_workflow, make_revalidation, session_factory
):
    """From ticket 14 the same code runs different thresholds, and an
    evaluation written before then can never have its own reconstructed."""
    _sync(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))

    evaluation_id = _revalidate(
        make_revalidation(quality_policy=QualityPolicy(negative_price_escalation_ratio=0.25))
    )

    with session_factory() as session:
        evaluation = session.get(QualityEvaluation, evaluation_id)

    assert evaluation.policy["negative_price_escalation_ratio"] == 0.25
    assert evaluation.policy["adjustment_tolerance_yen"] == "0.15"


def test_a_revalidation_is_refused_while_a_sync_holds_the_source(
    make_workflow, revalidation_workflow, session_factory
):
    """Queued behind a sync it would judge whatever that sync produced, which
    is not the snapshot the person was looking at."""
    _sync(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))
    workflow = make_workflow(FakeAdapter(trading_dates=OPEN_DAYS))
    run_id = workflow.start().id

    with pytest.raises(RevalidationConflict) as raised:
        revalidation_workflow.start()

    assert raised.value.active == {
        "kind": "sync",
        "sync_run_id": str(run_id),
        "status": "queued",
    }


def test_a_second_revalidation_is_refused_and_points_at_the_first(
    make_workflow, revalidation_workflow
):
    _sync(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))
    first = revalidation_workflow.start()

    with pytest.raises(RevalidationConflict) as raised:
        revalidation_workflow.start()

    assert raised.value.active["evaluation_id"] == str(first.id)


def test_a_sync_is_refused_while_a_revalidation_holds_the_source(
    make_workflow, revalidation_workflow
):
    """The other half of the exclusion: two things must not claim one source."""
    _sync(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))
    revalidation_workflow.start()

    with pytest.raises(SyncConflict):
        make_workflow(FakeAdapter(trading_dates=OPEN_DAYS)).start()


def test_syncing_is_available_again_once_the_revalidation_finishes(
    make_workflow, revalidation_workflow
):
    _sync(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))
    _revalidate(revalidation_workflow)

    assert make_workflow(FakeAdapter(trading_dates=OPEN_DAYS)).start().id is not None


def test_there_is_nothing_to_revalidate_before_the_first_sync(revalidation_workflow):
    with pytest.raises(RevalidationUnavailable):
        revalidation_workflow.start()


def test_a_broken_evaluator_leaves_the_head_where_it_was(
    make_workflow, revalidation_workflow, session_factory, monkeypatch
):
    """A crashed pass reached no verdict — neither "fine" nor "bad" — so it must
    not publish one. The failure is recorded on the evaluation instead."""
    import app.services.quality_pass as quality_pass

    _sync(make_workflow, FakeAdapter(trading_dates=OPEN_DAYS))
    with session_factory() as session:
        head_before = session.get(DataSnapshotHead, "jquants").snapshot_id

    monkeypatch.setattr(
        quality_pass, "evaluate", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    evaluation_id = revalidation_workflow.start().id
    with pytest.raises(RuntimeError):
        revalidation_workflow.execute(evaluation_id)

    with session_factory() as session:
        evaluation = session.get(QualityEvaluation, evaluation_id)
        assert session.get(DataSnapshotHead, "jquants").snapshot_id == head_before
        assert len(_snapshots(session)) == 1

    assert evaluation.status is QualityEvaluationStatus.FAILED
    assert "boom" in evaluation.error_summary
    # And the source is free again — a failed pass must not be a dead end.
    assert revalidation_workflow.start().id != evaluation_id


def test_the_api_queues_a_revalidation_and_reports_its_status(client, sync_workflow):
    run_id = sync_workflow.start().id
    sync_workflow._adapter = FakeAdapter(trading_dates=OPEN_DAYS)
    sync_workflow.execute(run_id)

    queued = client.post("/api/v1/quality/revalidations")
    latest = client.get("/api/v1/quality/revalidations/latest")

    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"
    assert queued.json()["kind"] == "revalidate"
    assert latest.json()["id"] == queued.json()["id"]


def test_the_api_says_what_is_holding_the_source(client, sync_workflow):
    run_id = sync_workflow.start().id
    sync_workflow._adapter = FakeAdapter(trading_dates=OPEN_DAYS)
    sync_workflow.execute(run_id)
    sync_workflow.start()

    response = client.post("/api/v1/quality/revalidations")

    assert response.status_code == 409
    assert response.json()["active_task"]["kind"] == "sync"


def test_the_api_reports_no_revalidation_before_the_first_one(client):
    assert client.get("/api/v1/quality/revalidations/latest").json() is None
