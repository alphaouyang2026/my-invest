"""anchor quality findings to evaluations

Findings used to hang off the sync run that produced them. Re-validation has no
sync run, and reusing the original run's id would collide with the findings that
run already wrote, so both producers now write *an evaluation* and findings
belong to it.

NON-DESTRUCTIVE, unlike the two migrations before it. Those purged because what
they dropped was worthless: rows the schema asserted a verdict about that
nothing had ever evaluated. The opposite holds here — the database carries about
2.14M daily bars that took roughly fourteen hours to fetch and *have* been
evaluated. So the existing sync gets an evaluation row built for it, its
findings are re-hung on that row, and not one market data row is touched.

The backfill is exact rather than best-effort: an evaluation is created only for
a run that demonstrably ran the pass — one that wrote findings, or that
published bars and went on to produce a snapshot. A run that failed before the
quality phase gets nothing, because asserting an evaluation that never happened
is the same class of lie the earlier purges existed to avoid.

Revision ID: 6fca8b9578af
Revises: 3fab64ea40a6
Create Date: 2026-08-16 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '6fca8b9578af'
down_revision: Union[str, None] = '3fab64ea40a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EVALUATION_KIND = sa.Enum('sync', 'revalidate', name='quality_evaluation_kind')
EVALUATION_STATUS = sa.Enum(
    'queued', 'running', 'succeeded', 'failed', name='quality_evaluation_status'
)

#: Runs that actually reached the contextual pass. `_complete` only calls it
#: when the run observed bars, and a completed pass either wrote findings or
#: went straight on to activate the snapshot.
_EVALUATED_RUNS = """
    SELECT r.id AS sync_run_id, r.source, s.id AS snapshot_id,
           COALESCE(s.created_at, r.finished_at, now()) AS created_at
    FROM sync_runs r
    LEFT JOIN data_snapshots s ON s.sync_run_id = r.id
    WHERE EXISTS (SELECT 1 FROM quality_findings f WHERE f.sync_run_id = r.id)
       OR (
            s.id IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM publication_bar_observations o
                JOIN endpoint_publications p ON p.id = o.publication_id
                WHERE p.sync_run_id = r.id
                  AND p.status = 'published'
                  AND p.endpoint = 'equities/bars/daily'
            )
          )
"""


def upgrade() -> None:
    # Both enums are created by create_table below — it emits CREATE TYPE for
    # any sa.Enum it carries. Creating them first duplicates the type.
    op.create_table(
        'quality_evaluation',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('kind', EVALUATION_KIND, nullable=False),
        sa.Column('source', sa.String(length=50), nullable=False),
        sa.Column('sync_run_id', sa.UUID(), nullable=True),
        sa.Column('task_id', sa.UUID(), nullable=True),
        sa.Column('produced_snapshot_id', sa.UUID(), nullable=True),
        sa.Column('policy', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('status', EVALUATION_STATUS, nullable=False),
        sa.Column('error_summary', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['sync_run_id'], ['sync_runs.id'], ),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
        sa.ForeignKeyConstraint(['produced_snapshot_id'], ['data_snapshots.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id'),
        sa.UniqueConstraint('produced_snapshot_id'),
    )
    op.create_index(op.f('ix_quality_evaluation_source'), 'quality_evaluation', ['source'], unique=False)
    op.create_index(op.f('ix_quality_evaluation_sync_run_id'), 'quality_evaluation', ['sync_run_id'], unique=False)

    # `policy` stays NULL: these passes ran before evaluations recorded their
    # thresholds, and writing today's defaults in would be a guess dressed as a
    # record.
    op.execute(
        f"""
        INSERT INTO quality_evaluation
            (id, kind, source, sync_run_id, produced_snapshot_id, status, created_at)
        SELECT gen_random_uuid(), 'sync', evaluated.source, evaluated.sync_run_id,
               evaluated.snapshot_id, 'succeeded', evaluated.created_at
        FROM ({_EVALUATED_RUNS}) AS evaluated
        """
    )

    op.add_column('quality_findings', sa.Column('evaluation_id', sa.UUID(), nullable=True))
    op.execute(
        """
        UPDATE quality_findings f
        SET evaluation_id = e.id
        FROM quality_evaluation e
        WHERE e.sync_run_id = f.sync_run_id
        """
    )
    op.alter_column('quality_findings', 'evaluation_id', nullable=False)

    op.drop_constraint(op.f('uq_quality_finding'), 'quality_findings', type_='unique')
    op.drop_index(op.f('ix_quality_findings_sync_run_id'), table_name='quality_findings')
    op.drop_column('quality_findings', 'sync_run_id')
    op.create_index(op.f('ix_quality_findings_evaluation_id'), 'quality_findings', ['evaluation_id'], unique=False)
    op.create_unique_constraint('uq_quality_finding', 'quality_findings', ['evaluation_id', 'rule', 'trade_date'])
    op.create_foreign_key(
        'fk_quality_finding_evaluation', 'quality_findings', 'quality_evaluation', ['evaluation_id'], ['id']
    )

    # A re-validated snapshot fetched nothing, so it has no run and no sync
    # mode. `mode` matters beyond tidiness: `_snapshot_watermarks` reads it to
    # decide when the next full reconcile is due.
    op.alter_column('data_snapshots', 'sync_run_id', existing_type=sa.UUID(), nullable=True)
    op.alter_column(
        'data_snapshots', 'mode',
        existing_type=postgresql.ENUM('initial', 'incremental', 'full_reconcile', name='sync_mode'),
        nullable=True,
    )


def downgrade() -> None:
    """DESTRUCTIVE in reverse: everything re-validation produced is dropped.

    The old shape has nowhere to put a finding without a sync run or a snapshot
    without one, so re-validated snapshots and their findings cannot survive
    the trip back. The head is walked back to the newest snapshot a sync
    produced first, which is a real regression in judgement, not just in shape.
    """
    op.execute(
        """
        DELETE FROM quality_findings f
        USING quality_evaluation e
        WHERE e.id = f.evaluation_id AND e.kind = 'revalidate'
        """
    )
    op.execute("DELETE FROM quality_evaluation WHERE kind = 'revalidate'")
    op.execute(
        """
        UPDATE data_snapshot_heads h
        SET snapshot_id = (
            SELECT s.id FROM data_snapshots s
            WHERE s.source = h.source AND s.sync_run_id IS NOT NULL
            ORDER BY s.version DESC LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1 FROM data_snapshots s2
            WHERE s2.id = h.snapshot_id AND s2.sync_run_id IS NULL
        )
        """
    )
    op.execute("DELETE FROM data_snapshots WHERE sync_run_id IS NULL")

    op.alter_column(
        'data_snapshots', 'mode',
        existing_type=postgresql.ENUM('initial', 'incremental', 'full_reconcile', name='sync_mode'),
        nullable=False,
    )
    op.alter_column('data_snapshots', 'sync_run_id', existing_type=sa.UUID(), nullable=False)

    op.add_column('quality_findings', sa.Column('sync_run_id', sa.UUID(), nullable=True))
    op.execute(
        """
        UPDATE quality_findings f
        SET sync_run_id = e.sync_run_id
        FROM quality_evaluation e
        WHERE e.id = f.evaluation_id
        """
    )
    op.alter_column('quality_findings', 'sync_run_id', nullable=False)
    op.drop_constraint('fk_quality_finding_evaluation', 'quality_findings', type_='foreignkey')
    op.drop_constraint('uq_quality_finding', 'quality_findings', type_='unique')
    op.drop_index(op.f('ix_quality_findings_evaluation_id'), table_name='quality_findings')
    op.drop_column('quality_findings', 'evaluation_id')
    op.create_index(op.f('ix_quality_findings_sync_run_id'), 'quality_findings', ['sync_run_id'], unique=False)
    op.create_unique_constraint('uq_quality_finding', 'quality_findings', ['sync_run_id', 'rule', 'trade_date'])
    op.create_foreign_key(
        'quality_findings_sync_run_id_fkey', 'quality_findings', 'sync_runs', ['sync_run_id'], ['id']
    )

    op.drop_index(op.f('ix_quality_evaluation_sync_run_id'), table_name='quality_evaluation')
    op.drop_index(op.f('ix_quality_evaluation_source'), table_name='quality_evaluation')
    op.drop_table('quality_evaluation')
    EVALUATION_STATUS.drop(op.get_bind())
    EVALUATION_KIND.drop(op.get_bind())
