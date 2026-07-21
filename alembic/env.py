"""Alembic async environment.

Migrations run over the DDL URL (owner role) — the runtime app role has no DDL
rights (§10.6). Model metadata is registered in Part 2 when
``app.core.database.Base`` lands.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import every module's models so their tables register on Base.metadata.
import app.modules.agents.models
import app.modules.appointments.models
import app.modules.auth.models
import app.modules.blog.models
import app.modules.content.models
import app.modules.favorites.models
import app.modules.leads.models
import app.modules.listings.models
import app.modules.media.models
import app.modules.notifications.models
import app.modules.reviews.models
import app.modules.tenants.models
import app.modules.users.models
import app.modules.valuations.models  # noqa: F401
from app.core.config import get_settings
from app.core.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return get_settings().database_ddl_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
