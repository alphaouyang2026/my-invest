"""Read-only view of the investable universe on a past decision day.

The pool is computed, never stored, so this route is the whole surface: it
exists so that "why is this security not in the pool" has an answer that does
not require reading the code. The pages that render any of it belong to ticket
06 — putting a block on the data-center page now would split the sync story
across two owners for no gain today.
"""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.market_data import DataSnapshot, DataSnapshotHead
from app.services.calendar_port import CalendarCoverageError, SessionCalendarPort
from app.services.stock_pool import (
    DEFAULT_POLICY,
    StockPoolPolicy,
    CalendarHistoryTooShortError,
    RosterUnavailableError,
    StockPool,
    StockPoolError,
    build_stock_pool,
)

router = APIRouter(prefix="/stock-pool", tags=["stock-pool"])

DEFAULT_SOURCE = "jquants"


def get_stock_pool_policy() -> StockPoolPolicy:
    """Injectable so a caller can judge under different thresholds without a
    restart — the same reason the policy is a dataclass and not `Settings`.
    Ticket 14 replaces this body with stored configuration."""
    return DEFAULT_POLICY


def _resolve_snapshot(db: Session, snapshot_id: uuid.UUID | None) -> DataSnapshot:
    if snapshot_id is not None:
        snapshot = db.get(DataSnapshot, snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Data snapshot not found")
        return snapshot
    head = db.scalar(select(DataSnapshotHead).where(DataSnapshotHead.source == DEFAULT_SOURCE))
    if head is None:
        raise HTTPException(status_code=404, detail="No data snapshot exists yet")
    return db.get(DataSnapshot, head.snapshot_id)


def _serialise(pool: StockPool) -> dict:
    return {
        "as_of": pool.as_of,
        "snapshot_id": str(pool.snapshot_id),
        # Echoed rather than left implicit: which roster answered is the single
        # fact that decides whether the membership is point-in-time or a guess.
        "master_snapshot_id": str(pool.master_snapshot_id),
        "master_as_of": pool.master_as_of,
        "policy_fingerprint": pool.policy_fingerprint,
        "member_count": len(pool.members),
        "members": [
            {
                "instrument_id": str(member.instrument_id),
                "symbol": member.symbol,
                "average_turnover": str(member.average_turnover),
            }
            for member in pool.members
        ],
        # Only securities that cleared the identity rules and then failed
        # something else, so this stays in the tens rather than the thousands
        # and needs no paging.
        "exclusions": [
            {
                "instrument_id": str(item.instrument_id),
                "symbol": item.symbol,
                "reasons": [reason.value for reason in item.reasons],
                "average_turnover": (
                    None if item.average_turnover is None else str(item.average_turnover)
                ),
            }
            for item in pool.exclusions
        ],
        "warnings": [
            {"code": warning.code.value, "detail": warning.detail} for warning in pool.warnings
        ],
    }


@router.get("")
def read_stock_pool(
    as_of: date = Query(..., description="Decision day; must be an open trading day"),
    snapshot_id: uuid.UUID | None = Query(
        None, description="Defaults to the current head snapshot"
    ),
    db: Session = Depends(get_db),
    policy: StockPoolPolicy = Depends(get_stock_pool_policy),
) -> dict:
    snapshot = _resolve_snapshot(db, snapshot_id)
    try:
        calendar = SessionCalendarPort(db, snapshot.calendar_publication_id)
        pool = build_stock_pool(db, snapshot, as_of=as_of, calendar=calendar, policy=policy)
    except (CalendarHistoryTooShortError, RosterUnavailableError) as exc:
        # 409, not 400: the request is well formed and will succeed unchanged
        # once the data behind it reaches far enough back.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (StockPoolError, CalendarCoverageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialise(pool)
