from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Iterator

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import Connection, Engine


VERSION_TABLE = "alembic_version"
INITIAL_REVISION = "20260723_0001"
OPERATIONS_REVISION = "20260723_0002"
HEAD_REVISION = "20260723_0003"

INITIAL_TABLES = frozenset(
    {
        "access_credentials",
        "agent_instances",
        "api_credentials",
        "audit_events",
        "bot_profiles",
        "channel_accounts",
        "conversations",
        "delivery_jobs",
        "knowledge_bases",
        "memberships",
        "messages",
        "reply_jobs",
        "tenant_secrets",
        "tenants",
        "users",
    }
)
OPERATIONS_TABLES = INITIAL_TABLES | {
    "channel_contacts",
    "chat_policies",
    "friend_requests",
    "operation_attempts",
    "operation_jobs",
    "operation_limits",
}
HEAD_TABLES = OPERATIONS_TABLES | {"agent_activations"}

_LEGACY_REVISIONS = (
    (HEAD_REVISION, HEAD_TABLES),
    (OPERATIONS_REVISION, OPERATIONS_TABLES),
    (INITIAL_REVISION, INITIAL_TABLES),
)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _PROJECT_ROOT / "alembic.ini"
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_LOCK = threading.RLock()
_POSTGRES_LOCK_KEY = 6288512472671735111


class MigrationError(RuntimeError):
    pass


class LegacySchemaMismatch(MigrationError):
    pass


class SchemaDriftError(MigrationError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    current: tuple[str, ...]
    heads: tuple[str, ...]
    state: str
    versioned: bool
    at_head: bool
    schema_valid: bool
    legacy_revision: str | None = None
    schema_error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "current": list(self.current),
            "heads": list(self.heads),
            "state": self.state,
            "versioned": self.versioned,
            "at_head": self.at_head,
            "schema_valid": self.schema_valid,
            "legacy_revision": self.legacy_revision,
            "schema_error": self.schema_error,
        }


@dataclass(frozen=True, slots=True)
class MigrationResult:
    status: MigrationStatus
    baseline_revision: str | None
    upgraded: bool


def _alembic_config(connection: Connection | None = None) -> Config:
    config = Config(str(_ALEMBIC_INI)) if _ALEMBIC_INI.is_file() else Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def _heads() -> tuple[str, ...]:
    return tuple(sorted(ScriptDirectory.from_config(_alembic_config()).get_heads()))


def _application_tables(connection: Connection) -> frozenset[str]:
    return frozenset(inspect(connection).get_table_names()) - {VERSION_TABLE}


def _metadata_for(table_names: frozenset[str]) -> MetaData:
    from wechat_ai.tenant_store import Base

    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name in table_names:
            table.to_metadata(metadata)
    return metadata


def _current_model_tables() -> frozenset[str]:
    from wechat_ai.tenant_store import Base

    return frozenset(Base.metadata.tables)


def _schema_differences(
    connection: Connection,
    table_names: frozenset[str],
) -> list[object]:
    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": False,
            "include_object": lambda _object, name, item_type, _reflected, _compare_to: not (
                item_type == "table" and name == VERSION_TABLE
            ),
        },
    )
    return list(compare_metadata(context, _metadata_for(table_names)))


def _summarize_differences(differences: list[object]) -> str:
    rendered = "; ".join(repr(item) for item in differences[:8])
    if len(differences) > 8:
        rendered += f"; ... {len(differences) - 8} more"
    return rendered[:4000]


def _legacy_revision(connection: Connection) -> tuple[str | None, str]:
    actual_tables = _application_tables(connection)
    if not actual_tables:
        return None, ""
    for revision, expected_tables in _LEGACY_REVISIONS:
        if actual_tables != expected_tables:
            continue
        differences = _schema_differences(connection, expected_tables)
        if not differences:
            return revision, ""
        return None, _summarize_differences(differences)
    expected_shapes = ", ".join(
        f"{revision}={len(tables)} tables" for revision, tables in _LEGACY_REVISIONS
    )
    return None, (
        f"unrecognized table set ({len(actual_tables)} tables); "
        f"expected one of: {expected_shapes}"
    )


def _current_revisions(connection: Connection, versioned: bool) -> tuple[str, ...]:
    if not versioned:
        return ()
    context = MigrationContext.configure(connection)
    return tuple(sorted(context.get_current_heads()))


def _status(connection: Connection) -> MigrationStatus:
    all_tables = frozenset(inspect(connection).get_table_names())
    versioned = VERSION_TABLE in all_tables
    current = _current_revisions(connection, versioned)
    heads = _heads()
    at_head = bool(current) and set(current) == set(heads)

    if not versioned:
        if not _application_tables(connection):
            return MigrationStatus(
                current=(),
                heads=heads,
                state="empty",
                versioned=False,
                at_head=False,
                schema_valid=True,
            )
        legacy_revision, error = _legacy_revision(connection)
        return MigrationStatus(
            current=(),
            heads=heads,
            state="legacy" if legacy_revision else "incompatible",
            versioned=False,
            at_head=False,
            schema_valid=legacy_revision is not None,
            legacy_revision=legacy_revision,
            schema_error=error,
        )

    if not current:
        has_application_tables = bool(_application_tables(connection))
        return MigrationStatus(
            current=(),
            heads=heads,
            state="invalid" if has_application_tables else "empty",
            versioned=True,
            at_head=False,
            schema_valid=not has_application_tables,
            schema_error=(
                "version table has no revision while application tables exist"
                if has_application_tables
                else ""
            ),
        )

    if not at_head:
        expected_tables = (
            dict(_LEGACY_REVISIONS).get(current[0]) if len(current) == 1 else None
        )
        differences = (
            _schema_differences(connection, expected_tables)
            if expected_tables is not None
            else []
        )
        error = _summarize_differences(differences)
        return MigrationStatus(
            current=current,
            heads=heads,
            state="pending" if not differences else "drifted",
            versioned=True,
            at_head=False,
            schema_valid=not differences,
            schema_error=error,
        )

    differences = _schema_differences(connection, _current_model_tables())
    error = _summarize_differences(differences)
    return MigrationStatus(
        current=current,
        heads=heads,
        state="current" if not differences else "drifted",
        versioned=True,
        at_head=True,
        schema_valid=not differences,
        schema_error=error,
    )


def migration_status(engine: Engine) -> MigrationStatus:
    with engine.connect() as connection:
        return _status(connection)


@contextmanager
def _locked_connection(engine: Engine) -> Iterator[Connection]:
    with _MIGRATION_LOCK, engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _POSTGRES_LOCK_KEY},
            )
        yield connection


def upgrade_database(engine: Engine) -> MigrationResult:
    baseline_revision: str | None = None
    with _locked_connection(engine) as connection:
        before = _status(connection)
        if not before.schema_valid:
            error_type = SchemaDriftError if before.versioned else LegacySchemaMismatch
            raise error_type(before.schema_error or "center database schema is invalid")

        config = _alembic_config(connection)
        if before.state == "legacy":
            baseline_revision = before.legacy_revision
            command.stamp(config, baseline_revision)
        command.upgrade(config, "head")

        after = _status(connection)
        if not after.at_head or not after.schema_valid:
            raise SchemaDriftError(
                after.schema_error or "center database did not reach the migration head"
            )

    return MigrationResult(
        status=migration_status(engine),
        baseline_revision=baseline_revision,
        upgraded=before.current != after.current or baseline_revision is not None,
    )


__all__ = [
    "HEAD_REVISION",
    "HEAD_TABLES",
    "INITIAL_REVISION",
    "INITIAL_TABLES",
    "LegacySchemaMismatch",
    "MigrationError",
    "MigrationResult",
    "MigrationStatus",
    "OPERATIONS_REVISION",
    "OPERATIONS_TABLES",
    "SchemaDriftError",
    "migration_status",
    "upgrade_database",
]
