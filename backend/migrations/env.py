from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.db.base import Base
from app.models import *  # noqa: F401,F403  — registers models on Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # One transaction per revision rather than one for the whole upgrade.
            #
            # Postgres refuses to *use* an enum value added in the transaction
            # that is still open ("New enum values must be committed before they
            # can be used"), so a revision that adds a status label and a later
            # one that names it in a partial index cannot share a transaction.
            # Casting the predicate to text does not help — Postgres rejects it
            # with "functions in index predicate must be marked IMMUTABLE".
            #
            # The cost is that a failure midway through a multi-revision upgrade
            # leaves the earlier revisions applied. That is the normal alembic
            # posture, and `alembic current` reports exactly where it stopped.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
