"""What two producers of snapshots have to agree on about one source.

A sync and a re-validation both end by writing a `DataSnapshot` and moving the
head, and both must refuse to start while the other is in flight. Keeping the
lock, the "is anything running" question, the head pointer and the version
counter here is what stops each workflow growing its own slightly different
answer to the same four questions.

Nothing here knows about J-Quants, batches or quality rules; it is the narrow
seam the two workflows meet at.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.market_data import (
    ACTIVE_EVALUATION_STATUSES,
    DataSnapshot,
    DataSnapshotHead,
    QualityEvaluation,
    QualityEvaluationKind,
    SyncRun,
    SyncRunStatus,
)

ACTIVE_RUN_STATUSES = (SyncRunStatus.QUEUED, SyncRunStatus.RUNNING, SyncRunStatus.CANCELLING)


def _advisory_key(name: str) -> int:
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def lock_source(session: Session, source: str) -> None:
    """Transaction-level lock giving concurrent writers a predictable outcome.

    The partial unique index on `sync_runs` is the final guard for sync; this
    only makes the return value deterministic (§14). Re-validation has no such
    index, so for it this lock is also what keeps two simultaneous requests
    from both seeing an idle source.

    The key is still named after the sync because that is what it has always
    been called; both workflows must take *this* name or they would not
    contend at all.
    """
    session.execute(select(func.pg_advisory_xact_lock(_advisory_key(f"market-data-sync:{source}"))))


def active_sync_run(session: Session, source: str) -> SyncRun | None:
    """The run occupying this source, if any."""
    return session.scalar(
        select(SyncRun).where(SyncRun.source == source, SyncRun.status.in_(ACTIVE_RUN_STATUSES))
    )


def active_revalidation(session: Session, source: str) -> QualityEvaluation | None:
    """The re-validation occupying this source, if any.

    A sync's own evaluation is excluded: it runs inside a sync that already
    holds the source, and counting it would make the sync block itself.
    """
    return session.scalar(
        select(QualityEvaluation).where(
            QualityEvaluation.source == source,
            QualityEvaluation.kind == QualityEvaluationKind.REVALIDATE,
            QualityEvaluation.status.in_(ACTIVE_EVALUATION_STATUSES),
        )
    )


def set_head(
    session: Session,
    head: DataSnapshotHead | None,
    source: str,
    snapshot_id: uuid.UUID,
    when: datetime,
) -> None:
    if head is None:
        session.add(DataSnapshotHead(source=source, snapshot_id=snapshot_id, updated_at=when))
    else:
        head.snapshot_id = snapshot_id
        head.updated_at = when


def next_snapshot_version(session: Session, source: str) -> int:
    """Next per-source counter, taken under the head lock the caller already holds.

    A Postgres sequence cannot be per-source and would leave gaps on rollback,
    and a gap in a number shown to a person reads as lost data.
    """
    highest = session.scalar(
        select(func.max(DataSnapshot.version)).where(DataSnapshot.source == source)
    )
    return (highest or 0) + 1
