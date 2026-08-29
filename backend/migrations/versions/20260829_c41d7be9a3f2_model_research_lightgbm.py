"""model research: trained models, prediction runs, artifact publications

Revision ID: c41d7be9a3f2
Revises: b17c4e0a55d1
Create Date: 2026-08-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c41d7be9a3f2"
down_revision: Union[str, None] = "b17c4e0a55d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


experiment_kind = postgresql.ENUM("factor", "model", name="research_experiment_kind", create_type=False)
publication_status = postgresql.ENUM(
    "prepared", "committed", "failed", name="artifact_publication_status", create_type=False
)

#: The partial unique index that enforces "one active run per experiment" spells
#: its statuses out as literals, so adding phases means rebuilding it.
ACTIVE_STATUSES_BEFORE = (
    "'queued','waiting_for_bundle','computing_factors','computing_labels',"
    "'evaluating','publishing'"
)
ACTIVE_STATUSES_AFTER = (
    "'queued','waiting_for_bundle','computing_factors','computing_labels',"
    "'training','predicting','evaluating','publishing'"
)


def upgrade() -> None:
    # 'training' and 'predicting' were added by b17c4e0a55d1 and committed with
    # it, which is what lets the partial index below name them.
    experiment_kind.create(op.get_bind(), checkfirst=True)
    publication_status.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "research_experiments",
        sa.Column("kind", experiment_kind, nullable=False, server_default="factor"),
    )
    # Existing rows are all ticket 06 factor experiments; the default backfills
    # them, and is then dropped so a future insert has to say which kind it is.
    op.alter_column("research_experiments", "kind", server_default=None)

    # These four describe a momentum experiment and have no meaning for a model
    # one, whose definition lives entirely in the JSONB payload.
    for column in ("observation_start", "observation_end", "lookback_days", "skip_days"):
        op.alter_column("research_experiments", column, nullable=True)

    op.drop_index("uq_active_research_run_per_experiment", table_name="research_runs")
    op.create_index(
        "uq_active_research_run_per_experiment",
        "research_runs",
        ["experiment_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({ACTIVE_STATUSES_AFTER})"),
    )

    op.create_table(
        "trained_models",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("research_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_runs.id"), nullable=False, unique=True),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_experiments.id"), nullable=False),
        sa.Column("data_snapshot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("data_snapshots.id"), nullable=False),
        sa.Column("bundle_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("qlib_data_bundles.id")),
        sa.Column("feature_set_name", sa.String(60), nullable=False),
        sa.Column("feature_set_version", sa.String(30), nullable=False),
        sa.Column("label_definition", sa.String(80), nullable=False),
        sa.Column("train_start", sa.Date(), nullable=False),
        sa.Column("train_end", sa.Date(), nullable=False),
        sa.Column("valid_start", sa.Date(), nullable=False),
        sa.Column("valid_end", sa.Date(), nullable=False),
        sa.Column("test_start", sa.Date(), nullable=False),
        sa.Column("test_end", sa.Date(), nullable=False),
        sa.Column("fit_start", sa.Date(), nullable=False),
        sa.Column("fit_end", sa.Date(), nullable=False),
        sa.Column("model_params", postgresql.JSONB(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("best_iteration", sa.Integer()),
        sa.Column("runtime_identity", postgresql.JSONB(), nullable=False),
        sa.Column("inference_contract", postgresql.JSONB(), nullable=False),
        sa.Column("inference_contract_checksum", sa.String(64), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("model_checksum", sa.String(64), nullable=False),
        sa.Column("model_semantic_checksum", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_trained_models_experiment_id", "trained_models", ["experiment_id"])

    op.create_table(
        "prediction_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("research_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_runs.id"), nullable=False, unique=True),
        sa.Column("trained_model_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("trained_models.id"), nullable=False),
        sa.Column("data_snapshot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("data_snapshots.id"), nullable=False),
        sa.Column("prediction_start", sa.Date(), nullable=False),
        sa.Column("prediction_end", sa.Date(), nullable=False),
        sa.Column("cross_section_count", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("artifact_relative_path", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_prediction_runs_trained_model_id", "prediction_runs", ["trained_model_id"])

    op.create_table(
        "research_artifact_publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("research_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_runs.id"), nullable=False, unique=True),
        sa.Column("status", publication_status, nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("staging_path", sa.String(500)),
        sa.Column("logical_checksum", sa.String(64), nullable=False),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("research_artifact_publications")
    op.drop_index("ix_prediction_runs_trained_model_id", table_name="prediction_runs")
    op.drop_table("prediction_runs")
    op.drop_index("ix_trained_models_experiment_id", table_name="trained_models")
    op.drop_table("trained_models")

    op.drop_index("uq_active_research_run_per_experiment", table_name="research_runs")
    op.create_index(
        "uq_active_research_run_per_experiment",
        "research_runs",
        ["experiment_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({ACTIVE_STATUSES_BEFORE})"),
    )

    # Only safe because every surviving row is a factor experiment: a model one
    # has nothing to put in these columns, and the drop below removes the flag
    # that would let us tell them apart.
    op.execute(
        "DELETE FROM research_experiments WHERE kind = 'model'"
    )
    for column in ("observation_start", "observation_end", "lookback_days", "skip_days"):
        op.alter_column("research_experiments", column, nullable=False)
    op.drop_column("research_experiments", "kind")

    publication_status.drop(op.get_bind(), checkfirst=True)
    experiment_kind.drop(op.get_bind(), checkfirst=True)
    # `research_run_status` keeps its two new labels: Postgres cannot drop enum
    # values, and leaving them costs nothing.
