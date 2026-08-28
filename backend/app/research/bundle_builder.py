from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.market_data import BarVersion, DataSnapshot, TradingCalendar
from app.models.research import (
    BuildAttemptStatus,
    BundleStatus,
    DataBundleBuildAttempt,
    QlibDataBundle,
)
from app.research.bundle import BundleBar, build_instrument_frame, write_native_bundle
from app.research.qlib_runtime import read_features
from app.services.snapshot_reader import snapshot_member_query


EXPORTER_SCHEMA_VERSION = "1"
PYQLIB_VERSION = "0.9.7"

# Rows fetched per round trip while exporting. The point is the server-side
# cursor, not the number: a snapshot is millions of bars, and materialising
# them is what used to take the worker past the container's memory.
BAR_STREAM_CHUNK = 5000

# One line per N securities while exporting. There are thousands of them and
# each writes a directory of files, so per-security tracing is debug-only.
EXPORT_PROGRESS_EVERY = 500

logger = get_logger(__name__)


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


class QlibDataBundleBuilder:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def ensure(self, snapshot: DataSnapshot, *, task_id: uuid.UUID | None) -> QlibDataBundle:
        bundle = self.session.scalar(
            select(QlibDataBundle).where(
                QlibDataBundle.data_snapshot_id == snapshot.id,
                QlibDataBundle.exporter_schema_version == EXPORTER_SCHEMA_VERSION,
                QlibDataBundle.pyqlib_version == PYQLIB_VERSION,
            )
        )
        root = self.settings.qlib_data_dir.resolve()
        if bundle is not None and bundle.status == BundleStatus.READY and bundle.relative_path:
            if (root / bundle.relative_path).exists():
                bundle.last_used_at = datetime.now(timezone.utc)
                self.session.commit()
                logger.info(
                    "qlib_bundle.reused",
                    bundle_id=str(bundle.id),
                    instrument_count=bundle.instrument_count,
                    size_bytes=bundle.size_bytes,
                )
                return bundle
        if bundle is None:
            bundle = QlibDataBundle(
                data_snapshot_id=snapshot.id,
                exporter_schema_version=EXPORTER_SCHEMA_VERSION,
                pyqlib_version=PYQLIB_VERSION,
                status=BundleStatus.QUEUED,
            )
            self.session.add(bundle)
            self.session.flush()

        attempt = DataBundleBuildAttempt(
            bundle_id=bundle.id,
            task_id=task_id,
            status=BuildAttemptStatus.BUILDING,
            started_at=datetime.now(timezone.utc),
        )
        bundle.status = BundleStatus.BUILDING
        bundle.error_summary = None
        self.session.add(attempt)
        self.session.commit()

        root.mkdir(parents=True, exist_ok=True)
        temporary = root / f".{bundle.id}.tmp-{attempt.id}"
        final = root / str(bundle.id)
        temporary.mkdir()
        started = time.monotonic()
        logger.info(
            "qlib_bundle.build_started",
            bundle_id=str(bundle.id),
            build_attempt_id=str(attempt.id),
            data_snapshot_id=str(snapshot.id),
        )
        try:
            calendar = list(
                self.session.scalars(
                    select(TradingCalendar.trade_date)
                    .where(
                        TradingCalendar.publication_id == snapshot.calendar_publication_id,
                        TradingCalendar.is_open.is_(True),
                    )
                    .order_by(TradingCalendar.trade_date)
                ).all()
            )
            members = snapshot_member_query(snapshot.source, snapshot.bar_publish_sequence).subquery()
            row_count = self.session.scalar(select(func.count()).select_from(members)) or 0
            estimated_bytes = row_count * 20 * 4
            free_bytes = shutil.disk_usage(root).free
            if estimated_bytes > self.settings.qlib_disk_budget_bytes:
                raise RuntimeError("Estimated QlibDataBundle exceeds configured disk budget")
            if free_bytes < max(estimated_bytes * 2, 16 * 1024 * 1024):
                raise RuntimeError("Insufficient free disk space for atomic QlibDataBundle build")
            # Every security must carry the same columns, so which quality rules
            # the snapshot fired has to be known before the first frame is
            # built — one cheap pass over the array column instead of holding
            # every bar to find out.
            quality_fields = sorted(
                self.session.scalars(
                    select(distinct(func.unnest(BarVersion.quality_rules)))
                    .select_from(members)
                    .join(BarVersion, BarVersion.id == members.c.bar_version_id)
                ).all()
            )
            logger.info(
                "qlib_bundle.export_planned",
                trading_days=len(calendar),
                bar_rows=row_count,
                estimated_bytes=estimated_bytes,
                free_bytes=free_bytes,
                quality_fields=quality_fields,
            )
            exported = {"rows": 0}
            features = self._stream_instrument_frames(
                members, calendar=calendar, quality_fields=quality_fields, counter=exported
            )
            contents = write_native_bundle(temporary, calendar=calendar, features=features)
            logger.info(
                "qlib_bundle.exported",
                bar_rows=exported["rows"],
                instruments=len(contents.instruments),
                fields=len(contents.fields),
                seconds=round(time.monotonic() - started, 3),
            )
            bundle.status = BundleStatus.VALIDATING
            attempt.status = BuildAttemptStatus.VALIDATING
            self.session.commit()
            smoke = read_features(
                temporary,
                instruments="all",
                fields=["$close", "$factor"],
                start=calendar[0].isoformat(),
                end=calendar[-1].isoformat(),
            )
            if smoke.empty:
                raise RuntimeError("Qlib provider smoke test returned no data")
            logger.info("qlib_bundle.smoke_tested", rows=len(smoke))
            logical = {
                "snapshot_id": str(snapshot.id),
                "calendar": [item.isoformat() for item in calendar],
                "instruments": list(contents.instruments),
                "fields": list(contents.fields),
                "rows": exported["rows"],
            }
            checksum = hashlib.sha256(
                json.dumps(logical, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest()
            manifest = {**logical, "schema_version": EXPORTER_SCHEMA_VERSION, "logical_checksum": checksum}
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
            )
            bundle.status = BundleStatus.PUBLISHING
            attempt.status = BuildAttemptStatus.PUBLISHING
            self.session.commit()
            if final.exists():
                if root not in final.resolve().parents:
                    raise RuntimeError("QlibDataBundle target escapes configured root")
                shutil.rmtree(final)
            os.replace(temporary, final)
            bundle.status = BundleStatus.READY
            bundle.relative_path = str(bundle.id)
            bundle.logical_checksum = checksum
            bundle.manifest = manifest
            bundle.size_bytes = _directory_size(final)
            bundle.coverage_start = calendar[0]
            bundle.coverage_end = calendar[-1]
            bundle.instrument_count = len(contents.instruments)
            bundle.last_used_at = datetime.now(timezone.utc)
            attempt.status = BuildAttemptStatus.READY
            attempt.stats = {
                "rows": exported["rows"],
                "instruments": len(contents.instruments),
                "bytes": bundle.size_bytes,
            }
            attempt.finished_at = datetime.now(timezone.utc)
            self.session.commit()
            logger.info(
                "qlib_bundle.published",
                bundle_id=str(bundle.id),
                relative_path=bundle.relative_path,
                size_bytes=bundle.size_bytes,
                logical_checksum=checksum,
                seconds=round(time.monotonic() - started, 3),
            )
            return bundle
        except Exception as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            self._fail(bundle, attempt, str(exc))
            raise

    def _stream_instrument_frames(
        self,
        members,
        *,
        calendar: list,
        quality_fields: list[str],
        counter: dict[str, int],
    ) -> Iterator[tuple[str, pd.DataFrame]]:
        """Yield one security's frame at a time, holding only that security.

        Two things keep this bounded. The query selects columns rather than the
        `BarVersion` entity — loading entities would leave millions of them
        pinned in the session's identity map for the whole export — and the
        server-side cursor means the rows arrive in chunks instead of one list.
        Ordering by instrument then date is what lets a security be recognised
        as complete the moment the next one appears.
        """
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
        seen: list[uuid.UUID] = []
        pending: list[BundleBar] = []
        for row in self.session.execute(statement):
            if current is not None and row.instrument_id != current:
                yield str(current), build_instrument_frame(
                    calendar_index, pending, quality_fields=quality_fields
                )
                pending = []
            if current is None or row.instrument_id != current:
                yielded = len(seen)
                if yielded and yielded % EXPORT_PROGRESS_EVERY == 0:
                    logger.info(
                        "qlib_bundle.export_progress",
                        instruments_written=yielded,
                        bar_rows=counter["rows"],
                    )
                seen.append(row.instrument_id)
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

    def _fail(self, bundle: QlibDataBundle, attempt: DataBundleBuildAttempt, error: str) -> None:
        logger.error(
            "qlib_bundle.build_failed",
            bundle_id=str(bundle.id),
            build_attempt_id=str(attempt.id),
            error=error,
        )
        bundle.status = BundleStatus.FAILED
        bundle.error_summary = error
        attempt.status = BuildAttemptStatus.FAILED
        attempt.error_summary = error
        attempt.finished_at = datetime.now(timezone.utc)
        self.session.commit()
