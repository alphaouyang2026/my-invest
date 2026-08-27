import enum
import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import pg_enum


class BundleStatus(str, enum.Enum):
    QUEUED = "queued"
    BUILDING = "building"
    VALIDATING = "validating"
    PUBLISHING = "publishing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class BuildAttemptStatus(str, enum.Enum):
    QUEUED = "queued"
    BUILDING = "building"
    VALIDATING = "validating"
    PUBLISHING = "publishing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResearchRunStatus(str, enum.Enum):
    QUEUED = "queued"
    WAITING_FOR_BUNDLE = "waiting_for_bundle"
    COMPUTING_FACTORS = "computing_factors"
    COMPUTING_LABELS = "computing_labels"
    EVALUATING = "evaluating"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_RESEARCH_STATUSES = (
    ResearchRunStatus.QUEUED,
    ResearchRunStatus.WAITING_FOR_BUNDLE,
    ResearchRunStatus.COMPUTING_FACTORS,
    ResearchRunStatus.COMPUTING_LABELS,
    ResearchRunStatus.EVALUATING,
    ResearchRunStatus.PUBLISHING,
)

# "Something is supposed to be working on this right now." A bundle in one of
# these states blocks both a rebuild and a delete, so nothing may be left here
# once the worker that owned it is gone (see worker/runner.py).
ACTIVE_BUNDLE_STATUSES = (
    BundleStatus.QUEUED,
    BundleStatus.BUILDING,
    BundleStatus.VALIDATING,
    BundleStatus.PUBLISHING,
)

ACTIVE_BUILD_ATTEMPT_STATUSES = (
    BuildAttemptStatus.QUEUED,
    BuildAttemptStatus.BUILDING,
    BuildAttemptStatus.VALIDATING,
    BuildAttemptStatus.PUBLISHING,
)


class QlibDataBundle(Base):
    __tablename__ = "qlib_data_bundles"
    __table_args__ = (
        Index(
            "uq_qlib_bundle_identity",
            "data_snapshot_id",
            "exporter_schema_version",
            "pyqlib_version",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    data_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_snapshots.id"), nullable=False)
    exporter_schema_version: Mapped[str] = mapped_column(String(30), nullable=False)
    pyqlib_version: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[BundleStatus] = mapped_column(pg_enum(BundleStatus, "qlib_bundle_status"), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(String(500))
    logical_checksum: Mapped[str | None] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    coverage_start: Mapped[date | None] = mapped_column(Date)
    coverage_end: Mapped[date | None] = mapped_column(Date)
    instrument_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DataBundleBuildAttempt(Base):
    __tablename__ = "data_bundle_build_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bundle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("qlib_data_bundles.id"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    status: Mapped[BuildAttemptStatus] = mapped_column(pg_enum(BuildAttemptStatus, "bundle_build_attempt_status"), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchExperiment(Base):
    __tablename__ = "research_experiments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    definition_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    data_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_snapshots.id"), nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    observation_start: Mapped[date] = mapped_column(Date, nullable=False)
    observation_end: Mapped[date] = mapped_column(Date, nullable=False)
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False)
    skip_days: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchRun(Base):
    __tablename__ = "research_runs"
    __table_args__ = (
        Index(
            "uq_active_research_run_per_experiment",
            "experiment_id",
            unique=True,
            postgresql_where=text(
                "status IN ('queued','waiting_for_bundle','computing_factors','computing_labels','evaluating','publishing')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_experiments.id"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), unique=True, nullable=False)
    previous_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("research_runs.id"))
    bundle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qlib_data_bundles.id"))
    status: Mapped[ResearchRunStatus] = mapped_column(pg_enum(ResearchRunStatus, "research_run_status"), nullable=False)
    current_date: Mapped[date | None] = mapped_column(Date)
    processed_dates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_dates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    runtime_identity: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    warnings: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(Text)
    elapsed_seconds: Mapped[int | None] = mapped_column(Integer)
    peak_memory_bytes: Mapped[int | None] = mapped_column(BigInteger)
    artifact_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchArtifact(Base):
    __tablename__ = "research_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    research_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_runs.id"), unique=True, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(30), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    logical_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
