"""Alembic environment. Migrations are hand-written SQL (see migrations/sql/), so there is no
ORM metadata and no autogenerate."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from flighttracker.config import (
    MigrationSettings,
    check_database_url,
    resolve_ssl_root_cert,
)

config = context.config
if config.config_file_name is not None:
    # Don't disable loggers created before migrations ran (the app's, when run from tests).
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    # An explicit URL (used by the test suite) wins over the environment.
    # Otherwise FT_MIGRATION_DATABASE_URL, then FT_DATABASE_URL, from the environment or .env.
    url = config.attributes.get("database_url") or MigrationSettings().url()
    if not url:
        raise RuntimeError("Set FT_MIGRATION_DATABASE_URL (or FT_DATABASE_URL) to run migrations")
    return resolve_ssl_root_cert(check_database_url(url))


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
