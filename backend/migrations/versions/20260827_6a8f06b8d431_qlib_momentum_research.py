"""qlib data bundles and momentum research runs

Revision ID: 6a8f06b8d431
Revises: 2810aefa1b70
Create Date: 2026-08-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "6a8f06b8d431"
down_revision: Union[str, None] = "2810aefa1b70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


bundle_status = postgresql.ENUM(
    "queued", "building", "validating", "publishing", "ready", "failed", "deleted",
    name="qlib_bundle_status", create_type=False,
)
attempt_status = postgresql.ENUM(
    "queued", "building", "validating", "publishing", "ready", "failed", "cancelled",
    name="bundle_build_attempt_status", create_type=False,
)
run_status = postgresql.ENUM(
    "queued", "waiting_for_bundle", "computing_factors", "computing_labels",
    "evaluating", "publishing", "succeeded", "failed", "cancelled",
    name="research_run_status", create_type=False,
)


def upgrade() -> None:
    bundle_status.create(op.get_bind(), checkfirst=True)
    attempt_status.create(op.get_bind(), checkfirst=True)
    run_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "qlib_data_bundles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("data_snapshot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("data_snapshots.id"), nullable=False),
        sa.Column("exporter_schema_version", sa.String(30), nullable=False),
        sa.Column("pyqlib_version", sa.String(30), nullable=False),
        sa.Column("status", bundle_status, nullable=False),
        sa.Column("relative_path", sa.String(500)),
        sa.Column("logical_checksum", sa.String(64)),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("coverage_start", sa.Date()),
        sa.Column("coverage_end", sa.Date()),
        sa.Column("instrument_count", sa.Integer(), nullable=False),
        sa.Column("error_summary", sa.Text()),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "uq_qlib_bundle_identity", "qlib_data_bundles",
        ["data_snapshot_id", "exporter_schema_version", "pyqlib_version"], unique=True,
    )
    op.create_table(
        "data_bundle_build_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bundle_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("qlib_data_bundles.id"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id")),
        sa.Column("status", attempt_status, nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_summary", sa.Text()),
        sa.Column("stats", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_data_bundle_build_attempts_bundle_id", "data_bundle_build_attempts", ["bundle_id"])
    op.create_index("ix_data_bundle_build_attempts_task_id", "data_bundle_build_attempts", ["task_id"])
    op.create_table(
        "research_experiments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("definition_fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("data_snapshot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("data_snapshots.id"), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("observation_start", sa.Date(), nullable=False),
        sa.Column("observation_end", sa.Date(), nullable=False),
        sa.Column("lookback_days", sa.Integer(), nullable=False),
        sa.Column("skip_days", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "research_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_experiments.id"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False, unique=True),
        sa.Column("previous_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_runs.id")),
        sa.Column("bundle_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("qlib_data_bundles.id")),
        sa.Column("status", run_status, nullable=False),
        sa.Column("current_date", sa.Date()),
        sa.Column("processed_dates", sa.Integer(), nullable=False),
        sa.Column("total_dates", sa.Integer(), nullable=False),
        sa.Column("runtime_identity", postgresql.JSONB(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_summary", sa.Text()),
        sa.Column("elapsed_seconds", sa.Integer()),
        sa.Column("peak_memory_bytes", sa.BigInteger()),
        sa.Column("artifact_size_bytes", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_research_runs_experiment_id", "research_runs", ["experiment_id"])
    op.create_index(
        "uq_active_research_run_per_experiment", "research_runs", ["experiment_id"], unique=True,
        postgresql_where=sa.text(
            "status IN ('queued','waiting_for_bundle','computing_factors','computing_labels','evaluating','publishing')"
        ),
    )
    op.create_table(
        "research_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("research_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_runs.id"), nullable=False, unique=True),
        sa.Column("schema_version", sa.String(30), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("logical_checksum", sa.String(64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("research_artifacts")
    op.drop_index("uq_active_research_run_per_experiment", table_name="research_runs")
    op.drop_index("ix_research_runs_experiment_id", table_name="research_runs")
    op.drop_table("research_runs")
    op.drop_table("research_experiments")
    op.drop_index("ix_data_bundle_build_attempts_task_id", table_name="data_bundle_build_attempts")
    op.drop_index("ix_data_bundle_build_attempts_bundle_id", table_name="data_bundle_build_attempts")
    op.drop_table("data_bundle_build_attempts")
    op.drop_index("uq_qlib_bundle_identity", table_name="qlib_data_bundles")
    op.drop_table("qlib_data_bundles")
    run_status.drop(op.get_bind(), checkfirst=True)
    attempt_status.drop(op.get_bind(), checkfirst=True)
    bundle_status.drop(op.get_bind(), checkfirst=True)
