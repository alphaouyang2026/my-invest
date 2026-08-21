"""add trading calendar and data quality findings

Revision ID: 3fab64ea40a6
Revises: 9daf50e89e3c
Create Date: 2026-08-16 16:41:31.755327

DESTRUCTIVE: previously synced rows are purged, not migrated. Every existing
`bar_versions.quality_status` reads 'ok' by column default, but nothing ever
evaluated those rows — keeping them would leave the database asserting a
verdict it never reached. Re-syncing is cheap; a silent falsehood at the base
of every backtest is not. `instruments` is deliberately spared: it is the
stable identity layer, and nothing here invalidates an instrument_id.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '3fab64ea40a6'
down_revision: Union[str, None] = '9daf50e89e3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Truncated together, by name, with no CASCADE. Postgres refuses to truncate a
#: table while another table referencing it is left out of the list, so an
#: incomplete list fails loudly here instead of quietly reaching into
#: `instruments` the way CASCADE would. Row-by-row DELETE was the first attempt
#: and is not viable: these tables hold ~8M rows and over 2GB between them.
_PURGE_TABLES = (
    "current_bars",
    "publication_bar_observations",
    "bar_versions",
    "bar_records",
    "data_snapshot_heads",
    "data_snapshots",
    "raw_source_pages",
    "instrument_master_snapshot_members",
    "instrument_master_snapshots",
    "sync_target_dates",
    "sync_batches",
    "endpoint_publications",
    "sync_runs",
)

_SYNC_PHASES_BEFORE = (
    "discovering_calendar",
    "planning",
    "bars",
    "master",
    "activating_snapshot",
    "complete",
)


def _purge_previous_sync_data() -> None:
    op.execute(f"TRUNCATE TABLE {', '.join(_PURGE_TABLES)}")
    # Not truncatable: only this task type goes, and `tasks` carries other
    # rows. Safe once the publications referencing it are already gone.
    op.execute("DELETE FROM tasks WHERE task_type = 'jquants_sync'")


def _rebuild_sync_phase(values: tuple[str, ...]) -> None:
    """Replace the sync_phase enum wholesale.

    Postgres can add an enum value in place but never remove one, so the
    downgrade has to recreate the type. Safe here only because the rows that
    could hold the value being dropped were purged above.
    """
    members = ", ".join(f"'{value}'" for value in values)
    op.execute("ALTER TYPE sync_phase RENAME TO sync_phase_old")
    op.execute(f"CREATE TYPE sync_phase AS ENUM ({members})")
    op.execute(
        "ALTER TABLE sync_runs ALTER COLUMN phase TYPE sync_phase USING phase::text::sync_phase"
    )
    op.execute("DROP TYPE sync_phase_old")


def upgrade() -> None:
    _purge_previous_sync_data()

    # Autogenerate cannot see a new member of an existing enum, so this is
    # hand-written and easy to lose: SyncPhase.EVALUATING_QUALITY sits between
    # MASTER and ACTIVATING_SNAPSHOT.
    op.execute("ALTER TYPE sync_phase ADD VALUE IF NOT EXISTS 'evaluating_quality' AFTER 'master'")

    op.create_table('quality_findings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('sync_run_id', sa.UUID(), nullable=False),
    sa.Column('rule', sa.String(length=40), nullable=False),
    sa.Column('trade_date', sa.Date(), nullable=False),
    sa.Column('severity', sa.Enum('warning', 'rejecting', name='quality_severity'), nullable=False),
    sa.Column('affected_count', sa.Integer(), nullable=False),
    sa.Column('evaluated_count', sa.Integer(), nullable=False),
    sa.Column('sample', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['sync_run_id'], ['sync_runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sync_run_id', 'rule', 'trade_date', name='uq_quality_finding')
    )
    op.create_index(op.f('ix_quality_findings_sync_run_id'), 'quality_findings', ['sync_run_id'], unique=False)
    op.create_table('trading_calendar',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('publication_id', sa.UUID(), nullable=False),
    sa.Column('market', sa.String(length=20), nullable=False),
    sa.Column('trade_date', sa.Date(), nullable=False),
    sa.Column('is_open', sa.Boolean(), nullable=False),
    sa.Column('session', sa.String(length=20), nullable=True),
    sa.Column('hol_div', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['publication_id'], ['endpoint_publications.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('publication_id', 'market', 'trade_date', name='uq_trading_calendar_day')
    )
    op.create_index(op.f('ix_trading_calendar_publication_id'), 'trading_calendar', ['publication_id'], unique=False)

    op.add_column('bar_versions', sa.Column('quality_rules', postgresql.ARRAY(sa.String(length=40)), nullable=False))
    # alter_column does not emit CREATE TYPE the way create_table does, and the
    # varchar -> enum cast needs an explicit USING even on an empty table.
    sa.Enum('ok', 'excluded', 'untradable', name='bar_quality_status').create(op.get_bind())
    op.alter_column('bar_versions', 'quality_status',
               existing_type=sa.VARCHAR(length=30),
               type_=sa.Enum('ok', 'excluded', 'untradable', name='bar_quality_status'),
               existing_nullable=False,
               postgresql_using='quality_status::bar_quality_status')

    op.add_column('data_snapshots', sa.Column('is_backtest_eligible', sa.Boolean(), nullable=False))
    op.add_column('data_snapshots', sa.Column('version', sa.Integer(), nullable=False))

    # Leftover from an earlier migration: the column carries a comment whose
    # literal text is "comment". The model declares none, so this drops it.
    op.alter_column('instruments', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment=None,
               existing_comment='comment',
               existing_nullable=False,
               existing_server_default=sa.text('now()'))


def downgrade() -> None:
    op.alter_column('instruments', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='comment',
               existing_nullable=False,
               existing_server_default=sa.text('now()'))
    op.drop_column('data_snapshots', 'version')
    op.drop_column('data_snapshots', 'is_backtest_eligible')
    op.alter_column('bar_versions', 'quality_status',
               existing_type=sa.Enum('ok', 'excluded', 'untradable', name='bar_quality_status'),
               type_=sa.VARCHAR(length=30),
               existing_nullable=False,
               postgresql_using='quality_status::text')
    # Autogenerate never emits DROP TYPE for a native enum, and without it the
    # downgrade -> upgrade round trip dies with DuplicateObject.
    sa.Enum(name='bar_quality_status').drop(op.get_bind())
    op.drop_column('bar_versions', 'quality_rules')
    op.drop_index(op.f('ix_trading_calendar_publication_id'), table_name='trading_calendar')
    op.drop_table('trading_calendar')
    op.drop_index(op.f('ix_quality_findings_sync_run_id'), table_name='quality_findings')
    op.drop_table('quality_findings')
    sa.Enum(name='quality_severity').drop(op.get_bind())

    _purge_previous_sync_data()
    _rebuild_sync_phase(_SYNC_PHASES_BEFORE)
