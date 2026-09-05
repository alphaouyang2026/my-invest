"""Stateless PostgreSQL/DataSnapshot to Qlib day-provider export.

This module owns the expensive conversion rules.  The productised bundle
builder and the direct experiment are adapters around this single seam; neither
is allowed to carry a second copy of the bar-to-Qlib mapping.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.market_data import BarVersion, DataSnapshot, TradingCalendar
from app.research.bundle import BundleBar, build_instrument_frame, write_native_bundle
from app.services.snapshot_reader import snapshot_member_query

EXPORTER_SCHEMA_VERSION = "2"
PYQLIB_VERSION = "0.9.7"
BAR_STREAM_CHUNK = 5000
EXPORT_PROGRESS_EVERY = 500

logger = get_logger(__name__)


@dataclass(frozen=True)
class DayProviderManifest:
    snapshot_id: str
    snapshot_bar_publish_sequence: int
    calendar_publication_id: str
    exporter_schema_version: str
    pyqlib_version: str
    coverage_start: date
    coverage_end: date
    instrument_count: int
    fields: tuple[str, ...]
    logical_checksum: str
    rows: int
    instruments: tuple[str, ...]
    calendar: tuple[date, ...]

    @property
    def schema_version(self) -> str:
        """Compatibility name used by the existing 07 bundle rows."""
        return self.exporter_schema_version

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_bar_publish_sequence": self.snapshot_bar_publish_sequence,
            "calendar_publication_id": self.calendar_publication_id,
            "exporter_schema_version": self.exporter_schema_version,
            "pyqlib_version": self.pyqlib_version,
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "instrument_count": self.instrument_count,
            "fields": list(self.fields),
            "logical_checksum": self.logical_checksum,
            "rows": self.rows,
            "instruments": list(self.instruments),
            "calendar": [item.isoformat() for item in self.calendar],
            # Kept for consumers of the original ticket-07 manifest.
            "schema_version": self.exporter_schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "DayProviderManifest":
        try:
            calendar = tuple(date.fromisoformat(str(item)) for item in payload["calendar"])
            instruments = tuple(str(item) for item in payload["instruments"])
            fields = tuple(str(item) for item in payload["fields"])
            schema = str(payload.get("exporter_schema_version") or payload["schema_version"])
            coverage_start = date.fromisoformat(
                str(payload.get("coverage_start") or calendar[0].isoformat())
            )
            coverage_end = date.fromisoformat(
                str(payload.get("coverage_end") or calendar[-1].isoformat())
            )
            return cls(
                snapshot_id=str(payload["snapshot_id"]),
                snapshot_bar_publish_sequence=int(payload.get("snapshot_bar_publish_sequence", 0)),
                calendar_publication_id=str(payload.get("calendar_publication_id", "")),
                exporter_schema_version=schema,
                pyqlib_version=str(payload.get("pyqlib_version", PYQLIB_VERSION)),
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                instrument_count=int(payload.get("instrument_count", len(instruments))),
                fields=fields,
                logical_checksum=str(payload["logical_checksum"]),
                rows=int(payload["rows"]),
                instruments=instruments,
                calendar=calendar,
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ValueError(f"Invalid day-provider manifest: {exc}") from exc


def load_day_provider_manifest(path: Path) -> DayProviderManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read day-provider manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Day-provider manifest {path} must contain a JSON object")
    return DayProviderManifest.from_dict(payload)


def export_snapshot_day_provider(
    session: Session,
    snapshot: DataSnapshot,
    destination: Path,
) -> DayProviderManifest:
    """Write one complete native day provider and its deterministic manifest."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError(f"Destination must be empty: {destination}")

    calendar = tuple(
        session.scalars(
            select(TradingCalendar.trade_date)
            .where(
                TradingCalendar.publication_id == snapshot.calendar_publication_id,
                TradingCalendar.is_open.is_(True),
            )
            .order_by(TradingCalendar.trade_date)
        ).all()
    )
    if not calendar:
        raise ValueError(f"Snapshot {snapshot.id} has no open trading days")

    members = snapshot_member_query(snapshot.source, snapshot.bar_publish_sequence).subquery()
    quality_fields = sorted(
        session.scalars(
            select(distinct(func.unnest(BarVersion.quality_rules)))
            .select_from(members)
            .join(BarVersion, BarVersion.id == members.c.bar_version_id)
        ).all()
    )
    counter = {"rows": 0}
    contents = write_native_bundle(
        destination,
        calendar=calendar,
        features=_stream_instrument_frames(
            session,
            members,
            calendar=calendar,
            quality_fields=quality_fields,
            counter=counter,
        ),
    )
    if not contents.instruments:
        raise ValueError(f"Snapshot {snapshot.id} exported no readable instruments")

    logical = {
        "snapshot_id": str(snapshot.id),
        "calendar": [item.isoformat() for item in calendar],
        "instruments": list(contents.instruments),
        "fields": list(contents.fields),
        "rows": counter["rows"],
    }
    checksum = hashlib.sha256(
        json.dumps(logical, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    manifest = DayProviderManifest(
        snapshot_id=str(snapshot.id),
        snapshot_bar_publish_sequence=snapshot.bar_publish_sequence,
        calendar_publication_id=str(snapshot.calendar_publication_id),
        exporter_schema_version=EXPORTER_SCHEMA_VERSION,
        pyqlib_version=PYQLIB_VERSION,
        coverage_start=calendar[0],
        coverage_end=calendar[-1],
        instrument_count=len(contents.instruments),
        fields=contents.fields,
        logical_checksum=checksum,
        rows=counter["rows"],
        instruments=contents.instruments,
        calendar=calendar,
    )
    (destination / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    logger.info(
        "day_provider.exported",
        snapshot_id=str(snapshot.id),
        rows=manifest.rows,
        instruments=manifest.instrument_count,
        fields=len(manifest.fields),
    )
    return manifest


def _stream_instrument_frames(
    session: Session,
    members,
    *,
    calendar: tuple[date, ...],
    quality_fields: list[str],
    counter: dict[str, int],
) -> Iterator[tuple[str, pd.DataFrame]]:
    calendar_index = pd.Index(calendar)
    statement = (
        select(
            members.c.instrument_id,
            members.c.trade_date,
            BarVersion.raw_open,
            BarVersion.raw_high,
            BarVersion.raw_low,
            BarVersion.raw_close,
            BarVersion.raw_volume,
            BarVersion.adjusted_open,
            BarVersion.adjusted_high,
            BarVersion.adjusted_low,
            BarVersion.adjusted_close,
            BarVersion.adjusted_volume,
            BarVersion.trading_value,
            BarVersion.adjustment_factor,
            BarVersion.quality_status,
            BarVersion.quality_rules,
        )
        .join(BarVersion, BarVersion.id == members.c.bar_version_id)
        .order_by(members.c.instrument_id, members.c.trade_date)
        .execution_options(yield_per=BAR_STREAM_CHUNK)
    )

    current: uuid.UUID | None = None
    yielded = 0
    pending: list[BundleBar] = []
    for row in session.execute(statement):
        if current is not None and row.instrument_id != current:
            yield str(current), build_instrument_frame(
                calendar_index, pending, quality_fields=quality_fields
            )
            yielded += 1
            if yielded % EXPORT_PROGRESS_EVERY == 0:
                logger.info(
                    "day_provider.export_progress",
                    instruments_written=yielded,
                    bar_rows=counter["rows"],
                )
            pending = []
        current = row.instrument_id
        pending.append(
            BundleBar(
                instrument_id=row.instrument_id,
                trade_date=row.trade_date,
                raw_open=row.raw_open,
                raw_high=row.raw_high,
                raw_low=row.raw_low,
                raw_close=row.raw_close,
                raw_volume=row.raw_volume,
                adjusted_open=row.adjusted_open,
                adjusted_high=row.adjusted_high,
                adjusted_low=row.adjusted_low,
                adjusted_close=row.adjusted_close,
                adjusted_volume=row.adjusted_volume,
                trading_value=row.trading_value,
                adjustment_factor=row.adjustment_factor,
                quality_status=row.quality_status.value,
                quality_rules=tuple(row.quality_rules),
            )
        )
        counter["rows"] += 1

    if current is not None:
        yield str(current), build_instrument_frame(
            calendar_index, pending, quality_fields=quality_fields
        )
