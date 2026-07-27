from __future__ import annotations

from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from wechat_ai.tenant_store import Base, JSON_VALUE, UTCDateTime


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


def _target_metadata():
    table_names = config.attributes.get("target_table_names")
    if table_names is None:
        return Base.metadata
    metadata = Base.metadata.__class__()
    for table in Base.metadata.sorted_tables:
        if table.name in table_names:
            table.to_metadata(metadata)
    return metadata


def _render_item(item_type, item, autogen_context):
    if item_type == "type" and isinstance(item, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    if item_type == "type" and item is JSON_VALUE:
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return "sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')"
    return False


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=_target_metadata(),
        compare_type=True,
        compare_server_default=False,
        render_item=_render_item,
    )


def _database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url", "").strip()
    database_url = configured or os.environ.get("CENTER_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("CENTER_DATABASE_URL is required for direct Alembic commands")
    return database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=_target_metadata(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        _configure(supplied_connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    config.set_main_option("sqlalchemy.url", _database_url().replace("%", "%%"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
