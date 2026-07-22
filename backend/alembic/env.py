from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from backend.app.modules.final_cut_review.infra.database import Base, database_connect_args
from backend.app.modules.final_cut_review.infra import sqlalchemy_models  # noqa: F401
from backend.app.settings import get_database_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import engine_from_config, pool

    settings = get_database_settings()
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = settings.database_url
    connect_args = database_connect_args(settings)
    if not settings.database_url.startswith("sqlite"):
        connect_args["options"] = f"-c statement_timeout={settings.database_migration_statement_timeout_ms}"
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
