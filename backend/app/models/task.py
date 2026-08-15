import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import pg_enum


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(Base):
    """Generic background task record. The single worker process (see
    app/worker) polls for QUEUED tasks and executes them serially — no Redis,
    no Kafka, no distributed task system (spec §5.3)."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[TaskStatus] = mapped_column(
        pg_enum(TaskStatus, "task_status"),
        nullable=False,
        default=TaskStatus.QUEUED,
        index=True,
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Counts how many times this task has been claimed, including re-queues
    # after a worker restart. Bounds crash-recovery retries and stamps the
    # provenance of each publication — it is not a fencing token, since the
    # single-worker deployment invariant makes fencing unnecessary.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # timestamptz throughout: the system stores UTC internally and keeps the
    # market timezone alongside it (spec §11). A naive column would silently
    # drop the tzinfo off every timezone-aware datetime written to it.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
