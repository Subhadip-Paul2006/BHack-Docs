"""Alembic environment for PS-05.

Binds migrations to ``ps05.db.base.Base.metadata`` and supports both offline
and online modes. The configured ``DATABASE_URL`` (e.g.
``postgresql+asyncpg://...``) is used when no explicit ``sqlalchemy.url`` is
present in an ini file.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ps05 import db as ps05_db
from ps05.settings import get_settings

config = context.config

# Load logging config only when an alembic.ini explicitly defines one.
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except (KeyError, AttributeError):
        pass

settings = get_settings()
if settings.database_url and not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = ps05_db.Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no live connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the configured engine."""
    # NullPool: one short-lived connection for the migration unit of work.
    connectable = engine_from_config(
        config.get_section(config.config_main_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Async dialects (e.g. aiosqlite/asyncpg) must be driven via run_sync.
        if connectable.dialect.name.endswith("async") or str(connectable.url.drivername).endswith(
            ("+aiosqlite", "+asyncpg")
        ):
            connectable.run_sync(do_run_migrations)
        else:
            do_run_migrations(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
