"""Data-snapshot and quality-finding HTTP surface.

Read-only. Snapshots are produced by a successful sync or by a re-validation,
never created by hand — a parallel manual route would be a second way to get
the same artefact — so this exposes what already exists rather than offering to
make more. Queueing a re-validation lives in `app.api.quality`.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.market_data import (
    DataSnapshot,
    DataSnapshotHead,
    QualityEvaluation,
    QualityFinding,
)

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


def _snapshot(snapshot: DataSnapshot, *, is_head: bool, evaluation_kind: str | None) -> dict:
    return {
        "id": str(snapshot.id),
        "source": snapshot.source,
        "version": snapshot.version,
        "mode": snapshot.mode.value if snapshot.mode else None,
        "sync_run_id": str(snapshot.sync_run_id) if snapshot.sync_run_id else None,
        # `sync` or `revalidate`: two snapshots with identical coverage are
        # otherwise indistinguishable, and "why did a new version appear when
        # nothing was fetched" is the first question they raise.
        "evaluation_kind": evaluation_kind,
        "coverage_start": snapshot.coverage_start,
        "coverage_end": snapshot.coverage_end,
        # The window this run actually re-checked at the source, as opposed to
        # the cumulative range it can read. Conflating the two is the mistake
        # the split exists to prevent. Null for a re-validation, which checked
        # nothing at the source.
        "verified_start": snapshot.verified_start,
        "verified_end": snapshot.verified_end,
        "is_backtest_eligible": snapshot.is_backtest_eligible,
        "is_current": is_head,
        "created_at": snapshot.created_at,
    }


def _finding(finding: QualityFinding) -> dict:
    return {
        "rule": finding.rule,
        "trade_date": finding.trade_date,
        "severity": finding.severity.value,
        "affected_count": finding.affected_count,
        "evaluated_count": finding.evaluated_count,
        "sample": finding.sample,
    }


def _evaluation_kinds(db: Session, snapshot_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """One query for the whole page rather than a lookup per snapshot."""
    if not snapshot_ids:
        return {}
    rows = db.execute(
        select(QualityEvaluation.produced_snapshot_id, QualityEvaluation.kind).where(
            QualityEvaluation.produced_snapshot_id.in_(snapshot_ids)
        )
    ).all()
    return {snapshot_id: kind.value for snapshot_id, kind in rows}


@router.get("")
def list_snapshots(db: Session = Depends(get_db)) -> list[dict]:
    heads = {head.snapshot_id for head in db.scalars(select(DataSnapshotHead)).all()}
    # Version breaks the tie: a re-validation can land in the same second as
    # the snapshot it re-judged, and without it the newer of the two would sort
    # arbitrarily — including above the one that supersedes it.
    snapshots = db.scalars(
        select(DataSnapshot)
        .order_by(DataSnapshot.created_at.desc(), DataSnapshot.version.desc())
        .limit(50)
    ).all()
    kinds = _evaluation_kinds(db, [item.id for item in snapshots])
    return [
        _snapshot(item, is_head=item.id in heads, evaluation_kind=kinds.get(item.id))
        for item in snapshots
    ]


@router.get("/{snapshot_id}")
def get_snapshot(snapshot_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """The snapshot plus every finding from the evaluation that produced it.

    Findings belong to an evaluation, and an evaluation names the snapshot it
    went on to produce — so this reads the pair rather than joining through the
    sync run, which a re-validated snapshot does not have. A snapshot whose
    verdict was inherited rather than evaluated (an empty incremental) has no
    evaluation, and correctly reports no findings.
    """
    snapshot = db.get(DataSnapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    head = db.scalar(
        select(DataSnapshotHead).where(DataSnapshotHead.snapshot_id == snapshot_id)
    )
    evaluation = db.scalar(
        select(QualityEvaluation).where(QualityEvaluation.produced_snapshot_id == snapshot_id)
    )
    findings = (
        db.scalars(
            select(QualityFinding)
            .where(QualityFinding.evaluation_id == evaluation.id)
            .order_by(QualityFinding.severity.desc(), QualityFinding.trade_date)
        ).all()
        if evaluation is not None
        else []
    )

    view = _snapshot(
        snapshot,
        is_head=head is not None,
        evaluation_kind=evaluation.kind.value if evaluation else None,
    )
    view["findings"] = [_finding(item) for item in findings]
    return view
