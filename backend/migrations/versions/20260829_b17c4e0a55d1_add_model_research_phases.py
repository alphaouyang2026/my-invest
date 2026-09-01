"""add training/predicting phases to research_run_status

Revision ID: b17c4e0a55d1
Revises: 6a8f06b8d431
Create Date: 2026-08-29

Its own revision on purpose. Postgres refuses to *use* an enum value that was
added in the still-open transaction:

    UnsafeNewEnumValueUsage: unsafe use of new value "training"
    HINT: New enum values must be committed before they can be used.

The next migration rebuilds `uq_active_research_run_per_experiment`, whose
partial predicate names both new labels, so the labels have to be committed by
then. Alembic runs each revision in its own transaction, which is exactly the
separation needed — splitting is cheaper and more obviously correct than
casting the predicate to text to dodge the enum.

This is the documented exception to model autogeneration: Alembic does not
detect native PostgreSQL enum label changes, so this revision contains only the
manual enum operation that autogenerate cannot emit.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "b17c4e0a55d1"
down_revision: Union[str, None] = "6a8f06b8d431"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Placed before 'evaluating' so the declaration order matches the order a
    # model run actually passes through. Ordering is cosmetic for correctness
    # but shows up in `ORDER BY status`.
    op.execute("ALTER TYPE research_run_status ADD VALUE IF NOT EXISTS 'training' BEFORE 'evaluating'")
    op.execute("ALTER TYPE research_run_status ADD VALUE IF NOT EXISTS 'predicting' BEFORE 'evaluating'")


def downgrade() -> None:
    # Postgres cannot remove an enum value. Leaving the two labels in place is
    # harmless: no row references them once the model tables are gone.
    pass
