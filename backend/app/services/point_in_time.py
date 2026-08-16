"""Point-in-time metadata, derived rather than stored.

Six of the seven fields the spec asks for already exist under the names 03
gave them, so this module documents the mapping instead of duplicating
columns:

    event_time      BarRecord.trade_date
    source          BarRecord.source
    ingested_at     BarVersion.first_seen_at
    publish_time    EndpointPublication.published_at  (via the observation)
    source_version  EndpointPublication.api_version + adapter_version
    effective_from  derived here
    effective_to    derived here

The validity interval is the one genuinely missing piece, and it is computed
rather than materialised. A single pair of columns on `BarVersion` would be
wrong, not merely redundant: reverting to earlier content reuses the original
version row, so one version can be the current belief over two disjoint
stretches. `version_intervals` returns both. The observations are the fact of
record; anything stored alongside them could only drift from them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.market_data import (
    EndpointPublication,
    PublicationBarObservation,
    PublicationStatus,
)


@dataclass(frozen=True)
class VersionInterval:
    """A stretch over which one version was what we believed.

    `effective_to` is None for the belief still in force. Sequence bounds are
    carried alongside the timestamps because a snapshot cutoff is expressed in
    publish sequence, so they are what a point-in-time query can join on.
    """

    bar_version_id: uuid.UUID
    effective_from: datetime
    effective_to: datetime | None
    from_sequence: int
    to_sequence: int | None


def version_intervals(session: Session, bar_record_id: uuid.UUID) -> list[VersionInterval]:
    """Every belief this bar record has held, oldest first."""
    rows = session.execute(
        select(
            PublicationBarObservation.bar_version_id,
            EndpointPublication.publish_sequence,
            EndpointPublication.published_at,
        )
        .join(
            EndpointPublication,
            EndpointPublication.id == PublicationBarObservation.publication_id,
        )
        .where(
            PublicationBarObservation.bar_record_id == bar_record_id,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
            EndpointPublication.publish_sequence.is_not(None),
        )
        .order_by(EndpointPublication.publish_sequence)
    ).all()

    intervals: list[VersionInterval] = []
    for version_id, sequence, published_at in rows:
        # An observation that saw the same content is not a new belief, it is
        # the same one being confirmed, so the open interval simply continues.
        if intervals and intervals[-1].bar_version_id == version_id and intervals[-1].effective_to is None:
            continue
        if intervals:
            intervals[-1] = _closed(intervals[-1], published_at, sequence)
        intervals.append(
            VersionInterval(
                bar_version_id=version_id,
                effective_from=published_at,
                effective_to=None,
                from_sequence=sequence,
                to_sequence=None,
            )
        )
    return intervals


def _closed(interval: VersionInterval, at: datetime, sequence: int) -> VersionInterval:
    return VersionInterval(
        bar_version_id=interval.bar_version_id,
        effective_from=interval.effective_from,
        effective_to=at,
        from_sequence=interval.from_sequence,
        to_sequence=sequence,
    )
