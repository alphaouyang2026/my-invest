"""The one place that knows how a DataSnapshot resolves bar versions.

A snapshot stores a cutoff on the publish sequence rather than a copy of every
bar row. Resolving "which version did this snapshot see" therefore means
picking, per bar record, the observation from the highest-sequenced PUBLISHED
publication at or below the cutoff. Callers must not reassemble that rule
themselves (docs/design/jquants-continuous-batch-sync.md §9.1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.market_data import (
    BarRecord,
    BarVersion,
    DataSnapshot,
    EndpointPublication,
    PublicationBarObservation,
    PublicationStatus,
)


@dataclass(frozen=True)
class SnapshotCoverage:
    start: date | None
    end: date | None
    member_count: int


def snapshot_member_query(source: str, bar_publish_sequence: int) -> Select:
    """Per bar record, the version visible at the given cutoff.

    DISTINCT ON keeps this a single index-friendly pass instead of a
    correlated max-subquery per record.
    """
    return (
        select(
            BarRecord.id.label("bar_record_id"),
            BarRecord.instrument_id,
            BarRecord.trade_date,
            BarRecord.session,
            PublicationBarObservation.bar_version_id,
            EndpointPublication.publish_sequence,
        )
        .join(PublicationBarObservation, PublicationBarObservation.bar_record_id == BarRecord.id)
        .join(EndpointPublication, EndpointPublication.id == PublicationBarObservation.publication_id)
        .where(
            BarRecord.source == source,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
            EndpointPublication.publish_sequence.is_not(None),
            EndpointPublication.publish_sequence <= bar_publish_sequence,
        )
        .distinct(BarRecord.id)
        .order_by(BarRecord.id, EndpointPublication.publish_sequence.desc())
    )


def resolve_coverage(session: Session, source: str, bar_publish_sequence: int) -> SnapshotCoverage:
    """Cumulative min/max trade date readable at the cutoff.

    This is what the activation transaction writes as the snapshot's declared
    coverage, so it must be derived from resolved members rather than from the
    dates this particular run happened to fetch.
    """
    members = snapshot_member_query(source, bar_publish_sequence).subquery()
    row = session.execute(
        select(
            func.min(members.c.trade_date),
            func.max(members.c.trade_date),
            func.count(),
        )
    ).one()
    return SnapshotCoverage(start=row[0], end=row[1], member_count=row[2])


def count_members_outside(
    session: Session,
    source: str,
    bar_publish_sequence: int,
    *,
    coverage_start: date,
    coverage_end: date,
) -> int:
    """Guard for the activation transaction: a snapshot must not declare a
    coverage range that some resolvable member falls outside of."""
    members = snapshot_member_query(source, bar_publish_sequence).subquery()
    return session.scalar(
        select(func.count()).where(
            (members.c.trade_date < coverage_start) | (members.c.trade_date > coverage_end)
        )
    )


def read_snapshot_bars(
    session: Session,
    snapshot: DataSnapshot,
    *,
    instrument_id: uuid.UUID | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[BarVersion]:
    """Bar content as frozen by this snapshot — the read path for research,
    backtests and replay."""
    members = snapshot_member_query(snapshot.source, snapshot.bar_publish_sequence).subquery()
    query = select(BarVersion, members.c.trade_date, members.c.instrument_id).join(
        members, members.c.bar_version_id == BarVersion.id
    )
    if instrument_id is not None:
        query = query.where(members.c.instrument_id == instrument_id)
    if start is not None:
        query = query.where(members.c.trade_date >= start)
    if end is not None:
        query = query.where(members.c.trade_date <= end)
    query = query.order_by(members.c.trade_date, members.c.instrument_id)
    return list(session.execute(query).all())
