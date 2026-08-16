"""Market data sync model (docs/design/jquants-continuous-batch-sync.md).

The shape here follows one rule: a `SyncRun` owns an immutable plan, and every
batch inside it publishes atomically on its own. Bar *content* (`BarVersion`)
is therefore decoupled from the publication that observed it — reverting a
revision reuses the original version rather than rewriting audit history.
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import pg_enum


class SyncMode(str, enum.Enum):
    INITIAL = "initial"
    INCREMENTAL = "incremental"
    FULL_RECONCILE = "full_reconcile"


class SyncRunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    NO_CHANGE = "no_change"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset(
    {
        SyncRunStatus.SUCCEEDED,
        SyncRunStatus.NO_CHANGE,
        SyncRunStatus.PARTIAL_FAILED,
        SyncRunStatus.FAILED,
        SyncRunStatus.CANCELLED,
    }
)


class SyncPhase(str, enum.Enum):
    DISCOVERING_CALENDAR = "discovering_calendar"
    PLANNING = "planning"
    BARS = "bars"
    MASTER = "master"
    EVALUATING_QUALITY = "evaluating_quality"
    ACTIVATING_SNAPSHOT = "activating_snapshot"
    COMPLETE = "complete"


class SyncBatchStatus(str, enum.Enum):
    PENDING = "pending"
    STAGING = "staging"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SyncTargetStatus(str, enum.Enum):
    PENDING = "pending"
    STAGING = "staging"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublicationStatus(str, enum.Enum):
    STAGING = "staging"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BarQualityStatus(str, enum.Enum):
    """The row-local verdict on one bar version.

    Lives with the model rather than with the rules because it is persisted
    state, and `app.models` must not import `app.services`.
    """

    OK = "ok"
    #: A non-critical field is missing — readable, but excluded from pool
    #: construction and signals.
    EXCLUDED = "excluded"
    #: The security cannot be honestly priced or traded on this date.
    UNTRADABLE = "untradable"


class BarObservationDisposition(str, enum.Enum):
    NEW = "new"
    CHANGED = "changed"
    REVERTED = "reverted"
    UNCHANGED = "unchanged"


# Publish order across every endpoint generation. A snapshot freezes a cutoff
# on this sequence, which is what makes "the facts as of run X" answerable
# without copying every bar row per snapshot.
publish_sequence_seq = Sequence("endpoint_publish_seq", metadata=Base.metadata)


class SyncRun(Base):
    """One user-visible sync operation. Batches are an implementation detail;
    this row carries the frozen plan and the overall result."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        Index(
            "uq_sync_run_idempotency",
            "source",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_sync_run_active_source",
            "source",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'cancelling')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="jquants", nullable=False)
    mode: Mapped[SyncMode | None] = mapped_column(pg_enum(SyncMode, "sync_mode"), nullable=True)
    status: Mapped[SyncRunStatus] = mapped_column(
        pg_enum(SyncRunStatus, "sync_run_status"), default=SyncRunStatus.QUEUED, index=True
    )
    phase: Mapped[SyncPhase] = mapped_column(
        pg_enum(SyncPhase, "sync_phase"), default=SyncPhase.DISCOVERING_CALENDAR, nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200))

    batch_size: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    coverage_before: Mapped[date | None] = mapped_column(Date)
    planned_start: Mapped[date | None] = mapped_column(Date)
    planned_end: Mapped[date | None] = mapped_column(Date)
    plan_fingerprint: Mapped[str | None] = mapped_column(String(64))

    target_dates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_dates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_batches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_batches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_batch: Mapped[int | None] = mapped_column(Integer)

    pages_received: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rows_received: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rows_new: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rows_unchanged: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rows_changed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    actual_min: Mapped[date | None] = mapped_column(Date)
    actual_max: Mapped[date | None] = mapped_column(Date)

    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncBatch(Base):
    """One ordinal slice of the frozen plan — the atomic scope of a daily-bars
    publication."""

    __tablename__ = "sync_batches"
    __table_args__ = (
        UniqueConstraint("sync_run_id", "ordinal", name="uq_sync_batch_ordinal"),
        # Lets sync_target_dates carry a composite FK, so a target can never
        # point at a batch belonging to a different run.
        UniqueConstraint("id", "sync_run_id", name="uq_sync_batch_id_run"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sync_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sync_runs.id"), index=True, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[SyncBatchStatus] = mapped_column(
        pg_enum(SyncBatchStatus, "sync_batch_status"), default=SyncBatchStatus.PENDING, nullable=False
    )
    target_start: Mapped[date] = mapped_column(Date, nullable=False)
    target_end: Mapped[date] = mapped_column(Date, nullable=False)
    target_dates: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    published_publication_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("endpoint_publications.id", use_alter=True, name="fk_sync_batch_publication")
    )
    rows_received: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rows_new: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rows_unchanged: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rows_changed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncTargetDate(Base):
    __tablename__ = "sync_target_dates"
    __table_args__ = (
        UniqueConstraint("sync_run_id", "trade_date", name="uq_sync_target_date"),
        ForeignKeyConstraint(
            ["sync_batch_id", "sync_run_id"],
            ["sync_batches.id", "sync_batches.sync_run_id"],
            name="fk_sync_target_batch_run",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sync_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sync_runs.id"), index=True, nullable=False)
    sync_batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_calendar_code: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[SyncTargetStatus] = mapped_column(
        pg_enum(SyncTargetStatus, "sync_target_status"), default=SyncTargetStatus.PENDING, nullable=False
    )


class EndpointPublication(Base):
    """One endpoint generation. Invisible while STAGING; becomes official
    market fact only once PUBLISHED with a publish_sequence."""

    __tablename__ = "endpoint_publications"
    __table_args__ = (
        UniqueConstraint("sync_run_id", "endpoint", "scope_ordinal", "attempt", name="uq_publication_attempt"),
        CheckConstraint(
            "status = 'published' OR publish_sequence IS NULL",
            name="ck_publication_sequence_requires_published",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sync_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sync_runs.id"), index=True, nullable=False)
    sync_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sync_batches.id"), index=True)
    created_by_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    created_by_task_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    endpoint: Mapped[str] = mapped_column(String(100), nullable=False)
    scope_ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[PublicationStatus] = mapped_column(
        pg_enum(PublicationStatus, "publication_status"), default=PublicationStatus.STAGING
    )
    publish_sequence: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    api_version: Mapped[str] = mapped_column(String(20), default="v2")
    adapter_version: Mapped[str] = mapped_column(String(50))
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64))
    request_params: Mapped[dict] = mapped_column(JSONB, default=dict)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RawSourcePage(Base):
    __tablename__ = "raw_source_pages"
    __table_args__ = (UniqueConstraint("publication_id", "page_index", name="uq_raw_page"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("endpoint_publications.id"), index=True)
    page_index: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TradingCalendar(Base):
    """One publication owns a complete calendar; a revision never touches the
    rows an earlier snapshot resolves to.

    Bars need three tables to dedupe versions because they are enormous and
    mostly unchanged run to run. A calendar is ~250 rows a year, so copying the
    whole set per publication costs nothing and lets a snapshot resolve it
    through the `calendar_publication_id` pointer it already carries — no
    second `publish_sequence` resolution path.
    """

    __tablename__ = "trading_calendar"
    __table_args__ = (
        UniqueConstraint("publication_id", "market", "trade_date", name="uq_trading_calendar_day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("endpoint_publications.id"), nullable=False, index=True
    )
    # The source calendar carries no market dimension. "TSE" is the honest
    # label: HolDiv=3 means the OSE trades while TSE cash equities do not, so
    # naming the row TSE is what makes `is_open` true of anything.
    market: Mapped[str] = mapped_column(String(20), nullable=False, default="TSE")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    session: Mapped[str | None] = mapped_column(String(20))
    # Kept verbatim so a code we don't recognise stays visible in the data
    # rather than being flattened into "closed" with no trace.
    hol_div: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("source", "source_code", name="uq_instrument_source_code"),)

    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(50))
    source_code: Mapped[str] = mapped_column(String(20))
    exchange: Mapped[str] = mapped_column(String(20), default="TSE")
    currency: Mapped[str] = mapped_column(String(3), default="JPY")
    classification: Mapped[str] = mapped_column(String(50), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstrumentMasterSnapshot(Base):
    """Keyed by publication, not by (source, as_of_date, run): a failed master
    attempt must not block the same run from retrying."""

    __tablename__ = "instrument_master_snapshots"
    __table_args__ = (UniqueConstraint("publication_id", name="uq_master_snapshot_publication"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(50))
    as_of_date: Mapped[date] = mapped_column(Date)
    sync_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sync_runs.id"), index=True)
    publication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("endpoint_publications.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstrumentMasterSnapshotMember(Base):
    __tablename__ = "instrument_master_snapshot_members"
    __table_args__ = (UniqueConstraint("snapshot_id", "instrument_id", name="uq_snapshot_instrument"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instrument_master_snapshots.id"), index=True)
    instrument_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    company_name: Mapped[str | None] = mapped_column(String(300))
    company_name_en: Mapped[str | None] = mapped_column(String(300))
    market_code: Mapped[str] = mapped_column(String(20))
    market_name: Mapped[str | None] = mapped_column(String(100))
    sector_17: Mapped[str | None] = mapped_column(String(20))
    sector_33: Mapped[str | None] = mapped_column(String(20))
    scale_category: Mapped[str | None] = mapped_column(String(100))
    inferred_security_class: Mapped[str] = mapped_column(String(50))
    classification_method: Mapped[str] = mapped_column(String(100), default="jpx_code_suffix_v1")
    content_hash: Mapped[str] = mapped_column(String(64))


class BarRecord(Base):
    """The stable business identity of one daily bar fact. Content lives in
    BarVersion, so the identity survives every revision."""

    __tablename__ = "bar_records"
    __table_args__ = (
        UniqueConstraint("source", "instrument_id", "trade_date", "session", name="uq_bar_record_identity"),
        Index("ix_bar_record_source_date_instrument", "source", "trade_date", "instrument_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instruments.instrument_id"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    session: Mapped[str] = mapped_column(String(20), default="full_day", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BarVersion(Base):
    """Immutable bar content. Owns no publication link: A -> B -> A reuses the
    original A row instead of inserting duplicate content."""

    __tablename__ = "bar_versions"
    __table_args__ = (
        UniqueConstraint("bar_record_id", "content_hash", name="uq_bar_version_content"),
        # Supports the composite FK on publication_bar_observations.
        UniqueConstraint("id", "bar_record_id", name="uq_bar_version_id_record"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bar_record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bar_records.id"), index=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_open: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    raw_high: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    raw_low: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    raw_close: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    raw_volume: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    trading_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    adjusted_open: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    adjusted_high: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    adjusted_low: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    adjusted_close: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    adjusted_volume: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    adjustment_factor: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    # Row-local quality only. Set once, at insert: the verdict is a pure
    # function of this row's content, so reusing a version on an A -> B -> A
    # revert carries the right answer with it. Contextual rules cannot satisfy
    # that and live on the findings table instead.
    quality_status: Mapped[BarQualityStatus] = mapped_column(
        pg_enum(BarQualityStatus, "bar_quality_status"),
        default=BarQualityStatus.OK,
        nullable=False,
    )
    quality_rules: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), default=list, nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PublicationBarObservation(Base):
    """What one publication observed for one bar record. Written for every
    returned row — UNCHANGED included — so a snapshot can resolve versions by
    publish sequence alone."""

    __tablename__ = "publication_bar_observations"
    __table_args__ = (
        UniqueConstraint("publication_id", "bar_version_id", name="uq_observation_publication_version"),
        ForeignKeyConstraint(
            ["bar_version_id", "bar_record_id"],
            ["bar_versions.id", "bar_versions.bar_record_id"],
            name="fk_observation_version_record",
        ),
        Index("ix_observation_record_publication", "bar_record_id", "publication_id"),
    )

    publication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("endpoint_publications.id"), primary_key=True
    )
    bar_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bar_records.id"), primary_key=True
    )
    bar_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    disposition: Mapped[BarObservationDisposition] = mapped_column(
        pg_enum(BarObservationDisposition, "bar_observation_disposition"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CurrentBar(Base):
    """Read projection for "what is true now". Never the correctness source for
    a frozen backtest — that is DataSnapshot's job."""

    __tablename__ = "current_bars"

    bar_record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bar_records.id"), primary_key=True)
    bar_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bar_versions.id"), nullable=False)
    publication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("endpoint_publications.id"), nullable=False)
    publish_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class QualitySeverity(str, enum.Enum):
    #: Recorded and queryable, but never affects whether a snapshot is usable.
    WARNING = "warning"
    #: Enough to make the run's snapshot ineligible for backtesting.
    REJECTING = "rejecting"


class QualityEvaluationKind(str, enum.Enum):
    #: Ran as the final phase of a sync, over what that run published.
    SYNC = "sync"
    #: Ran on demand over the head snapshot, with today's rules.
    REVALIDATE = "revalidate"


class QualityEvaluationStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: A source may carry one of these at a time — a sync and a re-validation must
#: never be in flight together, since each changes what the other is judging.
ACTIVE_EVALUATION_STATUSES = frozenset(
    {QualityEvaluationStatus.QUEUED, QualityEvaluationStatus.RUNNING}
)


class QualityEvaluation(Base):
    """One pass of the contextual rules over one population of bars.

    Findings hang off this rather than off a sync run, because a re-validation
    has no run to hang them from and reusing the original run's id would
    collide with the findings that run already wrote. Both producers make *an
    evaluation*; the difference between them is `kind` and what `scope` the
    pass was given.

    Anchoring here rather than on the snapshot keeps the property 04 was
    careful about: an evaluation that never reached snapshot activation still
    leaves its reasons behind, with `produced_snapshot_id` left NULL.

    For a re-validation this row is also the progress record — there is no
    second state machine, because there are no phases to record: it is one set
    of aggregate queries, with none of the calendar/batch/paging/rate-limit
    machinery that makes `SyncRun` complicated.
    """

    __tablename__ = "quality_evaluation"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[QualityEvaluationKind] = mapped_column(
        pg_enum(QualityEvaluationKind, "quality_evaluation_kind"), nullable=False
    )
    #: Which market data source was judged. Derivable from `sync_run_id` for a
    #: sync, but a re-validation has none, and every other table that describes
    #: a body of market data carries it.
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sync_runs.id"), index=True)
    #: Set only for a re-validation, which is queued work of its own. A sync's
    #: task is reachable through its run.
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), unique=True)
    produced_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("data_snapshots.id"), unique=True
    )
    #: The `QualityPolicy` this pass ran under. Today production never injects
    #: one, so the code version says everything; from ticket 14 on, thresholds
    #: become user-editable and the same code will produce different verdicts.
    #: Recorded now because it costs one column, and because an evaluation
    #: written before that lands can never have its thresholds reconstructed.
    #: NULL means "written before evaluations recorded this".
    policy: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[QualityEvaluationStatus] = mapped_column(
        pg_enum(QualityEvaluationStatus, "quality_evaluation_status"),
        default=QualityEvaluationStatus.QUEUED,
        nullable=False,
    )
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QualityFinding(Base):
    """One rule's verdict for one trade date within one evaluation.

    Aggregated per date because that is the grain the escalation threshold is
    computed at, and because per-instrument rows would run to millions over two
    years. `sample` keeps a capped list so an investigation has somewhere to
    start; the row-local detail stays recoverable from `bar_versions`.
    """

    __tablename__ = "quality_findings"
    __table_args__ = (
        UniqueConstraint("evaluation_id", "rule", "trade_date", name="uq_quality_finding"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_evaluation.id"), nullable=False, index=True
    )
    rule: Mapped[str] = mapped_column(String(40), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    severity: Mapped[QualitySeverity] = mapped_column(
        pg_enum(QualitySeverity, "quality_severity"), nullable=False
    )
    affected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The population the rule actually examined — the denominator behind any
    #: escalation, kept so the decision can be re-checked rather than trusted.
    evaluated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sample: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DataSnapshot(Base):
    """Immutable research view created only when a whole run succeeds.

    `coverage_*` is the cumulative readable range resolved at the cutoff;
    `verified_*` is only the window this run actually re-checked at the source.
    Conflating the two is the mistake this split exists to prevent.
    """

    __tablename__ = "data_snapshots"
    __table_args__ = (UniqueConstraint("sync_run_id", name="uq_data_snapshot_run"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    #: NULL for a snapshot produced by a re-validation: it fetched nothing, so
    #: claiming a run would be a lie, and the unique constraint would reject a
    #: borrowed one anyway. `QualityEvaluation.produced_snapshot_id` is how
    #: such a snapshot is traced back to what made it.
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sync_runs.id"))
    #: NULL for the same reason, and not merely for tidiness: `mode` is what
    #: `_snapshot_watermarks` reads to decide when the next full reconcile is
    #: due, so inheriting `initial` here would quietly postpone it.
    mode: Mapped[SyncMode | None] = mapped_column(pg_enum(SyncMode, "sync_mode"))
    bar_publish_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    calendar_publication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("endpoint_publications.id"), nullable=False
    )
    master_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instrument_master_snapshots.id"), nullable=False
    )
    coverage_start: Mapped[date] = mapped_column(Date, nullable=False)
    coverage_end: Mapped[date] = mapped_column(Date, nullable=False)
    verified_start: Mapped[date | None] = mapped_column(Date)
    verified_end: Mapped[date | None] = mapped_column(Date)
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Decided once, at creation, and never revisited. A snapshot a backtest
    #: has already bound to must not have its verdict overturned by a later
    #: rule change, and a run that produced no rejecting finding was clean at
    #: the moment it was judged — which is the only moment that can be judged.
    is_backtest_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: A counter for humans, per source. Deliberately not the global publish
    #: sequence: gaps in that one would read as missing data.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DataSnapshotHead(Base):
    __tablename__ = "data_snapshot_heads"

    source: Mapped[str] = mapped_column(String(50), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_snapshots.id"), unique=True, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
