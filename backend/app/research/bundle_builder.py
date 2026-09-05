from __future__ import annotations

import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.market_data import DataSnapshot
from app.models.research import (
    BuildAttemptStatus,
    BundleStatus,
    DataBundleBuildAttempt,
    QlibDataBundle,
)
from app.research.day_provider_export import (
    EXPORTER_SCHEMA_VERSION,
    PYQLIB_VERSION,
    export_snapshot_day_provider,
)
from app.research.qlib_runtime import read_features
from app.services.snapshot_reader import snapshot_member_query


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
            members = snapshot_member_query(snapshot.source, snapshot.bar_publish_sequence).subquery()
            row_count = self.session.scalar(select(func.count()).select_from(members)) or 0
            estimated_bytes = row_count * 20 * 4
            free_bytes = shutil.disk_usage(root).free
            if estimated_bytes > self.settings.qlib_disk_budget_bytes:
                raise RuntimeError("Estimated QlibDataBundle exceeds configured disk budget")
            if free_bytes < max(estimated_bytes * 2, 16 * 1024 * 1024):
                raise RuntimeError("Insufficient free disk space for atomic QlibDataBundle build")
            logger.info(
                "qlib_bundle.export_planned",
                bar_rows=row_count,
                estimated_bytes=estimated_bytes,
                free_bytes=free_bytes,
            )
            exported = export_snapshot_day_provider(self.session, snapshot, temporary)
            logger.info(
                "qlib_bundle.exported",
                bar_rows=exported.rows,
                instruments=exported.instrument_count,
                fields=len(exported.fields),
                seconds=round(time.monotonic() - started, 3),
            )
            bundle.status = BundleStatus.VALIDATING
            attempt.status = BuildAttemptStatus.VALIDATING
            self.session.commit()
            smoke = read_features(
                temporary,
                instruments="all",
                fields=["$close", "$factor", "$vwap"],
                start=exported.coverage_start.isoformat(),
                end=exported.coverage_end.isoformat(),
            )
            if smoke.empty:
                raise RuntimeError("Qlib provider smoke test returned no data")
            logger.info("qlib_bundle.smoke_tested", rows=len(smoke))
            manifest = exported.to_dict()
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
            bundle.logical_checksum = exported.logical_checksum
            bundle.manifest = manifest
            bundle.size_bytes = _directory_size(final)
            bundle.coverage_start = exported.coverage_start
            bundle.coverage_end = exported.coverage_end
            bundle.instrument_count = exported.instrument_count
            bundle.last_used_at = datetime.now(timezone.utc)
            attempt.status = BuildAttemptStatus.READY
            attempt.stats = {
                "rows": exported.rows,
                "instruments": exported.instrument_count,
                "bytes": bundle.size_bytes,
            }
            attempt.finished_at = datetime.now(timezone.utc)
            self.session.commit()
            logger.info(
                "qlib_bundle.published",
                bundle_id=str(bundle.id),
                relative_path=bundle.relative_path,
                size_bytes=bundle.size_bytes,
                logical_checksum=exported.logical_checksum,
                seconds=round(time.monotonic() - started, 3),
            )
            return bundle
        except Exception as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            self._fail(bundle, attempt, str(exc))
            raise

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
