"""continuous batch sync model

Moves the J-Quants sync to the model in
docs/design/jquants-continuous-batch-sync.md: per-batch atomic publications,
bar identity split from bar content, and immutable DataSnapshots.

DESTRUCTIVE: previous sync rows are purged, not backfilled. Three new columns
(`bar_versions.bar_record_id`, `endpoint_publications.created_by_task_id`,
`sync_target_dates.sync_batch_id`) have no derivable value for rows written by
the old shape, and this schema has never carried data worth preserving. The
staged backfill in §17 of the design applies to a deployment that already has
history; re-sync instead.

Revision ID: 9daf50e89e3c
Revises: abffefb9fa61
Create Date: 2026-08-16 00:47:06.540852

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateSequence, DropSequence


revision: str = '9daf50e89e3c'
down_revision: Union[str, None] = 'abffefb9fa61'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PUBLISH_SEQUENCE = sa.Sequence("endpoint_publish_seq")

RUN_STATUSES = (
    "queued", "running", "cancelling", "succeeded",
    "no_change", "partial_failed", "failed", "cancelled",
)
OLD_RUN_STATUSES = (
    "queued", "running", "succeeded", "no_change", "partial_failed", "failed", "cancelled",
)


def _purge_previous_sync_data() -> None:
    for table in (
        "bar_versions",
        "raw_source_pages",
        "instrument_master_snapshot_members",
        "instrument_master_snapshots",
        "sync_target_dates",
        "endpoint_publications",
        "sync_runs",
    ):
        op.execute(f"DELETE FROM {table}")
    op.execute("DELETE FROM tasks WHERE task_type = 'jquants_sync'")


def _replace_run_status_enum(new_values: tuple[str, ...], old_values: tuple[str, ...]) -> None:
    """Swap the enum type wholesale.

    `ALTER TYPE ... ADD VALUE` cannot be used here: the partial index created
    below references the new label in the same transaction, which Postgres
    rejects.
    """
    labels = ", ".join(f"'{value}'" for value in new_values)
    op.execute("ALTER TYPE sync_run_status RENAME TO sync_run_status_old")
    op.execute(f"CREATE TYPE sync_run_status AS ENUM ({labels})")
    op.execute(
        "ALTER TABLE sync_runs ALTER COLUMN status TYPE sync_run_status "
        "USING status::text::sync_run_status"
    )
    op.execute("DROP TYPE sync_run_status_old")


def upgrade() -> None:
    _purge_previous_sync_data()
    _replace_run_status_enum(RUN_STATUSES, OLD_RUN_STATUSES)
    op.execute(CreateSequence(PUBLISH_SEQUENCE))

    op.create_table('bar_records',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('instrument_id', sa.UUID(), nullable=False),
    sa.Column('trade_date', sa.Date(), nullable=False),
    sa.Column('session', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.instrument_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'instrument_id', 'trade_date', 'session', name='uq_bar_record_identity')
    )
    op.create_index('ix_bar_record_source_date_instrument', 'bar_records', ['source', 'trade_date', 'instrument_id'], unique=False)
    op.create_table('sync_batches',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('sync_run_id', sa.UUID(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('pending', 'staging', 'published', 'failed', 'cancelled', name='sync_batch_status'), nullable=False),
    sa.Column('target_start', sa.Date(), nullable=False),
    sa.Column('target_end', sa.Date(), nullable=False),
    sa.Column('target_dates', sa.Integer(), nullable=False),
    sa.Column('attempt_count', sa.Integer(), nullable=False),
    sa.Column('published_publication_id', sa.UUID(), nullable=True),
    sa.Column('rows_received', sa.BigInteger(), nullable=False),
    sa.Column('rows_new', sa.BigInteger(), nullable=False),
    sa.Column('rows_unchanged', sa.BigInteger(), nullable=False),
    sa.Column('rows_changed', sa.BigInteger(), nullable=False),
    sa.Column('error_code', sa.String(length=100), nullable=True),
    sa.Column('error_summary', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['sync_run_id'], ['sync_runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'sync_run_id', name='uq_sync_batch_id_run'),
    sa.UniqueConstraint('sync_run_id', 'ordinal', name='uq_sync_batch_ordinal')
    )
    op.create_index(op.f('ix_sync_batches_sync_run_id'), 'sync_batches', ['sync_run_id'], unique=False)
    # sync_batches and endpoint_publications reference each other, so this leg
    # is added separately (the model marks it use_alter for the same reason).
    op.create_foreign_key('fk_sync_batch_publication', 'sync_batches', 'endpoint_publications', ['published_publication_id'], ['id'])

    op.add_column('bar_versions', sa.Column('bar_record_id', sa.UUID(), nullable=False))
    op.add_column('bar_versions', sa.Column('quality_status', sa.String(length=30), nullable=False))
    op.drop_index(op.f('ix_bar_versions_instrument_id'), table_name='bar_versions')
    op.drop_index(op.f('ix_bar_versions_is_current'), table_name='bar_versions')
    op.drop_index(op.f('ix_bar_versions_publication_id'), table_name='bar_versions')
    op.drop_index(op.f('ix_bar_versions_trade_date'), table_name='bar_versions')
    op.drop_constraint(op.f('uq_bar_version_content'), 'bar_versions', type_='unique')
    op.create_unique_constraint('uq_bar_version_content', 'bar_versions', ['bar_record_id', 'content_hash'])
    op.create_index(op.f('ix_bar_versions_bar_record_id'), 'bar_versions', ['bar_record_id'], unique=False)
    op.create_unique_constraint('uq_bar_version_id_record', 'bar_versions', ['id', 'bar_record_id'])
    op.drop_constraint(op.f('bar_versions_instrument_id_fkey'), 'bar_versions', type_='foreignkey')
    op.drop_constraint(op.f('bar_versions_publication_id_fkey'), 'bar_versions', type_='foreignkey')
    op.create_foreign_key('fk_bar_version_record', 'bar_versions', 'bar_records', ['bar_record_id'], ['id'])
    op.drop_column('bar_versions', 'is_current')
    op.drop_column('bar_versions', 'trade_date')
    op.drop_column('bar_versions', 'publication_id')
    op.drop_column('bar_versions', 'source')
    op.drop_column('bar_versions', 'instrument_id')
    op.drop_column('bar_versions', 'session')

    op.create_table('current_bars',
    sa.Column('bar_record_id', sa.UUID(), nullable=False),
    sa.Column('bar_version_id', sa.UUID(), nullable=False),
    sa.Column('publication_id', sa.UUID(), nullable=False),
    sa.Column('publish_sequence', sa.BigInteger(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['bar_record_id'], ['bar_records.id'], ),
    sa.ForeignKeyConstraint(['bar_version_id'], ['bar_versions.id'], ),
    sa.ForeignKeyConstraint(['publication_id'], ['endpoint_publications.id'], ),
    sa.PrimaryKeyConstraint('bar_record_id')
    )
    op.create_table('publication_bar_observations',
    sa.Column('publication_id', sa.UUID(), nullable=False),
    sa.Column('bar_record_id', sa.UUID(), nullable=False),
    sa.Column('bar_version_id', sa.UUID(), nullable=False),
    sa.Column('disposition', sa.Enum('new', 'changed', 'reverted', 'unchanged', name='bar_observation_disposition'), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['bar_record_id'], ['bar_records.id'], ),
    sa.ForeignKeyConstraint(['bar_version_id', 'bar_record_id'], ['bar_versions.id', 'bar_versions.bar_record_id'], name='fk_observation_version_record'),
    sa.ForeignKeyConstraint(['publication_id'], ['endpoint_publications.id'], ),
    sa.PrimaryKeyConstraint('publication_id', 'bar_record_id'),
    sa.UniqueConstraint('publication_id', 'bar_version_id', name='uq_observation_publication_version')
    )
    op.create_index('ix_observation_record_publication', 'publication_bar_observations', ['bar_record_id', 'publication_id'], unique=False)

    op.add_column('endpoint_publications', sa.Column('sync_batch_id', sa.UUID(), nullable=True))
    op.add_column('endpoint_publications', sa.Column('created_by_task_id', sa.UUID(), nullable=False))
    op.add_column('endpoint_publications', sa.Column('created_by_task_attempt', sa.Integer(), nullable=False))
    op.add_column('endpoint_publications', sa.Column('scope_ordinal', sa.Integer(), nullable=False))
    op.add_column('endpoint_publications', sa.Column('attempt', sa.Integer(), nullable=False))
    op.add_column('endpoint_publications', sa.Column('publish_sequence', sa.BigInteger(), nullable=True))
    op.add_column('endpoint_publications', sa.Column('error_code', sa.String(length=100), nullable=True))
    op.drop_constraint(op.f('uq_publication_run_endpoint'), 'endpoint_publications', type_='unique')
    op.create_index(op.f('ix_endpoint_publications_sync_batch_id'), 'endpoint_publications', ['sync_batch_id'], unique=False)
    op.create_unique_constraint('uq_publication_attempt', 'endpoint_publications', ['sync_run_id', 'endpoint', 'scope_ordinal', 'attempt'])
    op.create_unique_constraint('uq_publication_publish_sequence', 'endpoint_publications', ['publish_sequence'])
    op.create_foreign_key('fk_publication_batch', 'endpoint_publications', 'sync_batches', ['sync_batch_id'], ['id'])
    op.create_foreign_key('fk_publication_task', 'endpoint_publications', 'tasks', ['created_by_task_id'], ['id'])
    op.create_check_constraint('ck_publication_sequence_requires_published', 'endpoint_publications', "status = 'published' OR publish_sequence IS NULL")

    op.drop_constraint(op.f('uq_master_snapshot'), 'instrument_master_snapshots', type_='unique')
    op.create_unique_constraint('uq_master_snapshot_publication', 'instrument_master_snapshots', ['publication_id'])

    op.create_table('data_snapshots',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('sync_run_id', sa.UUID(), nullable=False),
    # sync_mode already exists from the previous revision.
    sa.Column('mode', postgresql.ENUM('initial', 'incremental', 'full_reconcile', name='sync_mode', create_type=False), nullable=False),
    sa.Column('bar_publish_sequence', sa.BigInteger(), nullable=False),
    sa.Column('calendar_publication_id', sa.UUID(), nullable=False),
    sa.Column('master_snapshot_id', sa.UUID(), nullable=False),
    sa.Column('coverage_start', sa.Date(), nullable=False),
    sa.Column('coverage_end', sa.Date(), nullable=False),
    sa.Column('verified_start', sa.Date(), nullable=True),
    sa.Column('verified_end', sa.Date(), nullable=True),
    sa.Column('plan_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['calendar_publication_id'], ['endpoint_publications.id'], ),
    sa.ForeignKeyConstraint(['master_snapshot_id'], ['instrument_master_snapshots.id'], ),
    sa.ForeignKeyConstraint(['sync_run_id'], ['sync_runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sync_run_id', name='uq_data_snapshot_run')
    )
    op.create_table('data_snapshot_heads',
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('snapshot_id', sa.UUID(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['snapshot_id'], ['data_snapshots.id'], ),
    sa.PrimaryKeyConstraint('source'),
    sa.UniqueConstraint('snapshot_id')
    )

    # Unlike create_table, add_column does not emit CREATE TYPE for its enum.
    sync_phase = postgresql.ENUM('discovering_calendar', 'planning', 'bars', 'master', 'activating_snapshot', 'complete', name='sync_phase')
    sync_phase.create(op.get_bind())
    op.add_column('sync_runs', sa.Column('phase', postgresql.ENUM('discovering_calendar', 'planning', 'bars', 'master', 'activating_snapshot', 'complete', name='sync_phase', create_type=False), nullable=False))
    op.add_column('sync_runs', sa.Column('idempotency_key', sa.String(length=200), nullable=True))
    op.add_column('sync_runs', sa.Column('batch_size', sa.Integer(), nullable=False))
    op.add_column('sync_runs', sa.Column('coverage_before', sa.Date(), nullable=True))
    op.add_column('sync_runs', sa.Column('planned_start', sa.Date(), nullable=True))
    op.add_column('sync_runs', sa.Column('planned_end', sa.Date(), nullable=True))
    op.add_column('sync_runs', sa.Column('plan_fingerprint', sa.String(length=64), nullable=True))
    op.add_column('sync_runs', sa.Column('total_batches', sa.Integer(), nullable=False))
    op.add_column('sync_runs', sa.Column('completed_batches', sa.Integer(), nullable=False))
    op.add_column('sync_runs', sa.Column('current_batch', sa.Integer(), nullable=True))
    op.add_column('sync_runs', sa.Column('cancel_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('sync_runs', sa.Column('error_code', sa.String(length=100), nullable=True))
    for column in ('pages_received', 'rows_received', 'rows_new', 'rows_unchanged', 'rows_changed'):
        op.alter_column('sync_runs', column, existing_type=sa.INTEGER(), type_=sa.BigInteger(), existing_nullable=False)
    op.create_index('uq_sync_run_active_source', 'sync_runs', ['source'], unique=True, postgresql_where=sa.text("status IN ('queued', 'running', 'cancelling')"))
    op.create_index('uq_sync_run_idempotency', 'sync_runs', ['source', 'idempotency_key'], unique=True, postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.drop_column('sync_runs', 'requested_start')
    op.drop_column('sync_runs', 'current_endpoint')
    op.drop_column('sync_runs', 'requested_end')

    op.add_column('sync_target_dates', sa.Column('sync_batch_id', sa.UUID(), nullable=False))
    sync_target_status = postgresql.ENUM('pending', 'staging', 'published', 'failed', 'cancelled', name='sync_target_status')
    sync_target_status.create(op.get_bind())
    op.add_column('sync_target_dates', sa.Column('status', postgresql.ENUM('pending', 'staging', 'published', 'failed', 'cancelled', name='sync_target_status', create_type=False), nullable=False))
    op.create_index(op.f('ix_sync_target_dates_sync_batch_id'), 'sync_target_dates', ['sync_batch_id'], unique=False)
    op.create_foreign_key('fk_sync_target_batch_run', 'sync_target_dates', 'sync_batches', ['sync_batch_id', 'sync_run_id'], ['id', 'sync_run_id'])
    op.drop_column('sync_target_dates', 'fetch_status')

    # tasks is the one table that keeps its rows, so this column needs a
    # default to land; the model supplies the value from then on.
    op.add_column('tasks', sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'))
    op.alter_column('tasks', 'attempt_count', server_default=None)


def downgrade() -> None:
    """Restores the previous *shape* only — purged rows are not recoverable."""
    op.drop_column('tasks', 'attempt_count')
    op.add_column('sync_target_dates', sa.Column('fetch_status', sa.VARCHAR(length=20), autoincrement=False, nullable=False, server_default='pending'))
    op.drop_constraint('fk_sync_target_batch_run', 'sync_target_dates', type_='foreignkey')
    op.drop_index(op.f('ix_sync_target_dates_sync_batch_id'), table_name='sync_target_dates')
    op.drop_column('sync_target_dates', 'status')
    op.drop_column('sync_target_dates', 'sync_batch_id')
    op.execute("DROP TYPE IF EXISTS sync_target_status")

    op.add_column('sync_runs', sa.Column('requested_end', sa.DATE(), autoincrement=False, nullable=True))
    op.add_column('sync_runs', sa.Column('current_endpoint', sa.VARCHAR(length=100), autoincrement=False, nullable=True))
    op.add_column('sync_runs', sa.Column('requested_start', sa.DATE(), autoincrement=False, nullable=True))
    op.drop_index('uq_sync_run_idempotency', table_name='sync_runs', postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.drop_index('uq_sync_run_active_source', table_name='sync_runs', postgresql_where=sa.text("status IN ('queued', 'running', 'cancelling')"))
    for column in ('rows_changed', 'rows_unchanged', 'rows_new', 'rows_received', 'pages_received'):
        op.alter_column('sync_runs', column, existing_type=sa.BigInteger(), type_=sa.INTEGER(), existing_nullable=False)
    for column in (
        'error_code', 'cancel_requested_at', 'current_batch', 'completed_batches', 'total_batches',
        'plan_fingerprint', 'planned_end', 'planned_start', 'coverage_before', 'batch_size',
        'idempotency_key', 'phase',
    ):
        op.drop_column('sync_runs', column)
    op.execute("DROP TYPE IF EXISTS sync_phase")

    op.drop_table('data_snapshot_heads')
    op.drop_table('data_snapshots')

    op.drop_constraint('uq_master_snapshot_publication', 'instrument_master_snapshots', type_='unique')
    op.create_unique_constraint(op.f('uq_master_snapshot'), 'instrument_master_snapshots', ['source', 'as_of_date', 'sync_run_id'])

    op.drop_index('ix_observation_record_publication', table_name='publication_bar_observations')
    op.drop_table('publication_bar_observations')
    op.execute("DROP TYPE IF EXISTS bar_observation_disposition")
    op.drop_table('current_bars')

    op.drop_constraint('ck_publication_sequence_requires_published', 'endpoint_publications', type_='check')
    op.drop_constraint('fk_publication_task', 'endpoint_publications', type_='foreignkey')
    op.drop_constraint('fk_publication_batch', 'endpoint_publications', type_='foreignkey')
    op.drop_constraint('uq_publication_publish_sequence', 'endpoint_publications', type_='unique')
    op.drop_constraint('uq_publication_attempt', 'endpoint_publications', type_='unique')
    op.drop_index(op.f('ix_endpoint_publications_sync_batch_id'), table_name='endpoint_publications')
    op.create_unique_constraint(op.f('uq_publication_run_endpoint'), 'endpoint_publications', ['sync_run_id', 'endpoint'])
    for column in (
        'error_code', 'publish_sequence', 'attempt', 'scope_ordinal',
        'created_by_task_attempt', 'created_by_task_id', 'sync_batch_id',
    ):
        op.drop_column('endpoint_publications', column)

    op.add_column('bar_versions', sa.Column('session', sa.VARCHAR(length=20), autoincrement=False, nullable=False, server_default='full_day'))
    op.add_column('bar_versions', sa.Column('instrument_id', sa.UUID(), autoincrement=False, nullable=False))
    op.add_column('bar_versions', sa.Column('source', sa.VARCHAR(length=50), autoincrement=False, nullable=False, server_default='jquants'))
    op.add_column('bar_versions', sa.Column('publication_id', sa.UUID(), autoincrement=False, nullable=False))
    op.add_column('bar_versions', sa.Column('trade_date', sa.DATE(), autoincrement=False, nullable=False))
    op.add_column('bar_versions', sa.Column('is_current', sa.BOOLEAN(), autoincrement=False, nullable=False, server_default=sa.text('false')))
    op.drop_constraint('fk_bar_version_record', 'bar_versions', type_='foreignkey')
    op.create_foreign_key(op.f('bar_versions_publication_id_fkey'), 'bar_versions', 'endpoint_publications', ['publication_id'], ['id'])
    op.create_foreign_key(op.f('bar_versions_instrument_id_fkey'), 'bar_versions', 'instruments', ['instrument_id'], ['instrument_id'])
    op.drop_constraint('uq_bar_version_id_record', 'bar_versions', type_='unique')
    op.drop_index(op.f('ix_bar_versions_bar_record_id'), table_name='bar_versions')
    op.drop_constraint('uq_bar_version_content', 'bar_versions', type_='unique')
    op.create_unique_constraint(op.f('uq_bar_version_content'), 'bar_versions', ['source', 'instrument_id', 'trade_date', 'session', 'content_hash'])
    op.create_index(op.f('ix_bar_versions_trade_date'), 'bar_versions', ['trade_date'], unique=False)
    op.create_index(op.f('ix_bar_versions_publication_id'), 'bar_versions', ['publication_id'], unique=False)
    op.create_index(op.f('ix_bar_versions_is_current'), 'bar_versions', ['is_current'], unique=False)
    op.create_index(op.f('ix_bar_versions_instrument_id'), 'bar_versions', ['instrument_id'], unique=False)
    op.drop_column('bar_versions', 'quality_status')
    op.drop_column('bar_versions', 'bar_record_id')

    op.drop_index(op.f('ix_sync_batches_sync_run_id'), table_name='sync_batches')
    op.drop_table('sync_batches')
    op.execute("DROP TYPE IF EXISTS sync_batch_status")
    op.drop_index('ix_bar_record_source_date_instrument', table_name='bar_records')
    op.drop_table('bar_records')

    op.execute(DropSequence(PUBLISH_SEQUENCE))
    _replace_run_status_enum(OLD_RUN_STATUSES, RUN_STATUSES)
