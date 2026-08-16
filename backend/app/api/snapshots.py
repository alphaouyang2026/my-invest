"""Data-snapshot and quality-finding HTTP surface.

Read-only. Snapshots are produced by a successful sync, not created by hand —
a parallel manual route would be a second way to get the same artefact — so
this exposes what already exists rather than offering to make more.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.market_data import (
    DataSnapshot,
    DataSnapshotHead,
    QualityFinding,
)

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


def _snapshot(snapshot: DataSnapshot, *, is_head: bool) -> dict:
    return {
        "id": str(snapshot.id),
        "source": snapshot.source,
        "version": snapshot.version,
        "mode": snapshot.mode.value if snapshot.mode else None,
        "sync_run_id": str(snapshot.sync_run_id),
        "coverage_start": snapshot.coverage_start,
        "coverage_end": snapshot.coverage_end,
        # The window this run actually re-checked at the source, as opposed to
        # the cumulative range it can read. Conflating the two is the mistake
        # the split exists to prevent.
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


@router.get("")
def list_snapshots(db: Session = Depends(get_db)) -> list[dict]:
    heads = {head.snapshot_id for head in db.scalars(select(DataSnapshotHead)).all()}
    snapshots = db.scalars(
        select(DataSnapshot).order_by(DataSnapshot.created_at.desc()).limit(50)
    ).all()
    return [_snapshot(item, is_head=item.id in heads) for item in snapshots]


@router.get("/{snapshot_id}")
def get_snapshot(snapshot_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """The snapshot plus every finding from the run that produced it.

    Findings are anchored to the run rather than the snapshot — the pass runs
    before the snapshot exists — so this join is where the two meet.
    """
    snapshot = db.get(DataSnapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    head = db.scalar(
        select(DataSnapshotHead).where(DataSnapshotHead.snapshot_id == snapshot_id)
    )
    findings = db.scalars(
        select(QualityFinding)
        .where(QualityFinding.sync_run_id == snapshot.sync_run_id)
        .order_by(QualityFinding.severity.desc(), QualityFinding.trade_date)
    ).all()

    view = _snapshot(snapshot, is_head=head is not None)
    view["findings"] = [_finding(item) for item in findings]
    return view
