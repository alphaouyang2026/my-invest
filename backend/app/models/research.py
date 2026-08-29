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
    # Only a model run passes through these two. A factor run goes straight from
    # labels to evaluating, so the shared state machine gains values rather than
    # a second machine to keep in step with the first.
    TRAINING = "training"
    PREDICTING = "predicting"
    EVALUATING = "evaluating"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResearchExperimentKind(str, enum.Enum):
    """What an experiment researches; both kinds share one table and one
    fingerprint mechanism, and `kind` is part of the fingerprint payload so the
    two can never collide in it."""

    FACTOR = "factor"
    MODEL = "model"


class ArtifactPublicationStatus(str, enum.Enum):
    """Where a publication got to.

    `prepared` is written *before* the directory rename, so a worker that dies
    mid-publish leaves a row pointing at whatever is on disk instead of an
    orphaned directory nobody records.
    """

    PREPARED = "prepared"
    COMMITTED = "committed"
    FAILED = "failed"


ACTIVE_RESEARCH_STATUSES = (
    ResearchRunStatus.QUEUED,
    ResearchRunStatus.WAITING_FOR_BUNDLE,
    ResearchRunStatus.COMPUTING_FACTORS,
    ResearchRunStatus.COMPUTING_LABELS,
    ResearchRunStatus.TRAINING,
    ResearchRunStatus.PREDICTING,
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
    kind: Mapped[ResearchExperimentKind] = mapped_column(
        pg_enum(ResearchExperimentKind, "research_experiment_kind"),
        nullable=False,
        default=ResearchExperimentKind.FACTOR,
    )
    data_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_snapshots.id"), nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    observation_start: Mapped[date | None] = mapped_column(Date)
    observation_end: Mapped[date | None] = mapped_column(Date)
    lookback_days: Mapped[int | None] = mapped_column(Integer)
    skip_days: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchRun(Base):
    __tablename__ = "research_runs"
    __table_args__ = (
        Index(
            "uq_active_research_run_per_experiment",
            "experiment_id",
            unique=True,
            postgresql_where=text(
                "status IN ('queued','waiting_for_bundle','computing_factors','computing_labels',"
                "'training','predicting','evaluating','publishing')"
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


class TrainedModel(Base):
    """A model, addressable independently of the run that produced it.

    Kept apart from `ResearchRun` because ticket 12 has to score a *new*
    DataSnapshot with an already-trained model, and must not retrain to do it.
    Folded into the run, "reuse this model" would have nowhere to live.

    `inference_contract` is what makes that reuse safe: the feature set expansion,
    the processor identities, the label definition and the fit window, all in one
    immutable blob with its own checksum. A future inference path compiles the
    same contract from trusted code and compares — it never executes what is
    stored here (see `ModelExecutionSpec`), because a database column that
    reaches Qlib's config resolver is arbitrary code loading.
    """

    __tablename__ = "trained_models"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    research_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_runs.id"), unique=True, nullable=False
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_experiments.id"), nullable=False, index=True)
    data_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_snapshots.id"), nullable=False)
    bundle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qlib_data_bundles.id"))

    feature_set_name: Mapped[str] = mapped_column(String(60), nullable=False)
    feature_set_version: Mapped[str] = mapped_column(String(30), nullable=False)
    label_definition: Mapped[str] = mapped_column(String(80), nullable=False)

    train_start: Mapped[date] = mapped_column(Date, nullable=False)
    train_end: Mapped[date] = mapped_column(Date, nullable=False)
    valid_start: Mapped[date] = mapped_column(Date, nullable=False)
    valid_end: Mapped[date] = mapped_column(Date, nullable=False)
    test_start: Mapped[date] = mapped_column(Date, nullable=False)
    test_end: Mapped[date] = mapped_column(Date, nullable=False)
    #: Declared even while no fitted processor consumes it, so the contract is
    #: already correct when the first one is added (design section 7.4).
    fit_start: Mapped[date] = mapped_column(Date, nullable=False)
    fit_end: Mapped[date] = mapped_column(Date, nullable=False)

    #: Includes `num_threads`, unlike the fingerprint: useful for audit, and
    #: excluded from identity because it cannot change the trees.
    model_params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    #: A training *result*, never part of the definition.
    best_iteration: Mapped[int | None] = mapped_column(Integer)
    runtime_identity: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    inference_contract: Mapped[dict] = mapped_column(JSONB, nullable=False)
    inference_contract_checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    #: sha256 of the file as written. Differs across thread counts because
    #: LightGBM serialises `[num_threads: N]` into the text, so it proves
    #: integrity, not model identity.
    model_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    #: sha256 of the same file with the runtime parameter lines removed. Equal
    #: across thread counts, and therefore the thing to compare when asking
    #: whether two files are the same model.
    model_semantic_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PredictionRun(Base):
    """Scores from one trained model, written once, on successful publish only.

    No `status` column on purpose. An earlier draft had one while also calling
    the row immutable, which cannot both be true. Queueing, failure and
    cancellation belong to `ResearchRun`; this row exists only if the artifact
    it points at was committed.
    """

    __tablename__ = "prediction_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    research_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_runs.id"), unique=True, nullable=False
    )
    trained_model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trained_models.id"), nullable=False, index=True)
    data_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_snapshots.id"), nullable=False)
    prediction_start: Mapped[date] = mapped_column(Date, nullable=False)
    prediction_end: Mapped[date] = mapped_column(Date, nullable=False)
    cross_section_count: Mapped[int] = mapped_column(Integer, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchArtifactPublication(Base):
    """The record that closes the gap between a directory rename and a commit.

    `os.replace` and a Postgres transaction cannot commit together. Publishing
    directory-first leaves, on a crash, a published directory that no row
    mentions while recovery marks the run failed — a "failed run" with artifacts
    on disk, which the design forbids.

    So the row goes first, as `prepared`, naming the path and the checksum it
    expects. Then the rename. Then `committed` alongside the artifact rows, in
    one transaction. Recovery reads `prepared` rows and decides from the
    filesystem: complete the commit if the directory is there and matches the
    checksum, otherwise delete it and mark `failed`. Idempotent either way, so
    replaying it is safe.
    """

    __tablename__ = "research_artifact_publications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    research_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_runs.id"), unique=True, nullable=False
    )
    status: Mapped[ArtifactPublicationStatus] = mapped_column(
        pg_enum(ArtifactPublicationStatus, "artifact_publication_status"), nullable=False
    )
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    staging_path: Mapped[str | None] = mapped_column(String(500))
    logical_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
