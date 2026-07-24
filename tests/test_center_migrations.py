from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    inspect,
    select,
    text,
)

import main
from wechat_ai.center_migrations import (
    HEAD_REVISION,
    HEAD_TABLES,
    INITIAL_REVISION,
    INITIAL_TABLES,
    OPERATIONS_REVISION,
    OPERATIONS_TABLES,
    LegacySchemaMismatch,
    SchemaDriftError,
    migration_status,
)
from wechat_ai.tenant_store import Base, Tenant, TenantStore


class CenterMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TenantStore("sqlite+pysqlite:///:memory:")

    def tearDown(self) -> None:
        self.store.engine.dispose()

    def _create_unversioned_schema(self, table_names: frozenset[str]) -> None:
        Base.metadata.create_all(
            self.store.engine,
            tables=[Base.metadata.tables[name] for name in table_names],
        )

    def test_empty_database_upgrades_to_head_with_complete_model(self) -> None:
        before = migration_status(self.store.engine)

        self.store.create_schema()
        after = migration_status(self.store.engine)

        self.assertEqual(before.state, "empty")
        self.assertEqual(after.current, (HEAD_REVISION,))
        self.assertEqual(after.heads, (HEAD_REVISION,))
        self.assertTrue(after.at_head)
        self.assertTrue(after.schema_valid)
        self.assertEqual(
            set(inspect(self.store.engine).get_table_names()),
            set(Base.metadata.tables) | {"alembic_version"},
        )
        self.assertIn("agent_activations", inspect(self.store.engine).get_table_names())

        inspector = inspect(self.store.engine)
        for table in Base.metadata.sorted_tables:
            with self.subTest(table=table.name):
                self.assertEqual(
                    {column["name"] for column in inspector.get_columns(table.name)},
                    set(table.columns.keys()),
                )
                self.assertEqual(
                    set(inspector.get_pk_constraint(table.name)["constrained_columns"]),
                    {column.name for column in table.primary_key.columns},
                )
                expected_foreign_keys = {
                    (
                        tuple(constraint.column_keys),
                        tuple(element.target_fullname for element in constraint.elements),
                    )
                    for constraint in table.constraints
                    if isinstance(constraint, ForeignKeyConstraint)
                }
                actual_foreign_keys = {
                    (
                        tuple(constraint["constrained_columns"]),
                        tuple(
                            f"{constraint['referred_table']}.{column}"
                            for column in constraint["referred_columns"]
                        ),
                    )
                    for constraint in inspector.get_foreign_keys(table.name)
                }
                self.assertEqual(actual_foreign_keys, expected_foreign_keys)
                self.assertEqual(
                    {
                        constraint["name"]
                        for constraint in inspector.get_unique_constraints(table.name)
                    },
                    {
                        constraint.name
                        for constraint in table.constraints
                        if isinstance(constraint, UniqueConstraint)
                    },
                )
                self.assertEqual(
                    {
                        constraint["name"]
                        for constraint in inspector.get_check_constraints(table.name)
                    },
                    {
                        constraint.name
                        for constraint in table.constraints
                        if isinstance(constraint, CheckConstraint)
                    },
                )
                self.assertEqual(
                    {index["name"] for index in inspector.get_indexes(table.name)},
                    {index.name for index in table.indexes},
                )

    def test_package_migrations_run_without_repository_alembic_ini(self) -> None:
        missing_ini = Path(__file__).resolve().parent / "missing-alembic.ini"
        with patch("wechat_ai.center_migrations._ALEMBIC_INI", missing_ini):
            self.store.create_schema()

        status = migration_status(self.store.engine)
        self.assertEqual(status.current, (HEAD_REVISION,))
        self.assertTrue(status.schema_valid)

    def test_repeated_upgrade_is_idempotent_and_preserves_data(self) -> None:
        self.store.create_schema()
        tenant = self.store.create_tenant("persistent", "Persistent Tenant")

        self.store.create_schema()
        self.store.create_schema()

        with self.store.engine.connect() as connection:
            name = connection.scalar(select(Tenant.name).where(Tenant.id == tenant.id))
            version_rows = connection.scalar(text("SELECT COUNT(*) FROM alembic_version"))
        self.assertEqual(name, "Persistent Tenant")
        self.assertEqual(version_rows, 1)
        self.assertTrue(migration_status(self.store.engine).at_head)

    def test_initial_legacy_schema_is_stamped_then_upgraded_without_data_loss(self) -> None:
        self._create_unversioned_schema(INITIAL_TABLES)
        tenant = self.store.create_tenant("legacy", "Legacy Tenant")
        before = migration_status(self.store.engine)

        self.store.create_schema()

        self.assertEqual(before.state, "legacy")
        self.assertEqual(before.legacy_revision, INITIAL_REVISION)
        with self.store.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(Tenant.name).where(Tenant.id == tenant.id)),
                "Legacy Tenant",
            )
        self.assertEqual(migration_status(self.store.engine).current, (HEAD_REVISION,))
        self.assertEqual(
            set(inspect(self.store.engine).get_table_names()),
            set(HEAD_TABLES) | {"alembic_version"},
        )

    def test_unversioned_operations_schema_upgrades_from_second_revision(self) -> None:
        self._create_unversioned_schema(OPERATIONS_TABLES)

        before = migration_status(self.store.engine)
        self.store.create_schema()

        self.assertEqual(before.legacy_revision, OPERATIONS_REVISION)
        self.assertIn("agent_activations", inspect(self.store.engine).get_table_names())
        self.assertEqual(migration_status(self.store.engine).current, (HEAD_REVISION,))

    def test_unversioned_complete_schema_is_baselined_at_head(self) -> None:
        self._create_unversioned_schema(HEAD_TABLES)

        before = migration_status(self.store.engine)
        self.store.create_schema()

        self.assertEqual(before.legacy_revision, HEAD_REVISION)
        self.assertEqual(migration_status(self.store.engine).current, (HEAD_REVISION,))

    def test_unrecognized_legacy_schema_fails_without_stamping(self) -> None:
        incomplete = INITIAL_TABLES - {"audit_events"}
        self._create_unversioned_schema(incomplete)

        with self.assertRaisesRegex(LegacySchemaMismatch, "unrecognized table set"):
            self.store.create_schema()

        self.assertNotIn("alembic_version", inspect(self.store.engine).get_table_names())
        self.assertEqual(migration_status(self.store.engine).state, "incompatible")

    def test_legacy_table_set_with_missing_column_is_not_stamped(self) -> None:
        with self.store.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE tenants ("
                    "id VARCHAR(36) PRIMARY KEY, slug VARCHAR(100) NOT NULL UNIQUE, "
                    "status VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL, "
                    "updated_at DATETIME NOT NULL)"
                )
            )
        Base.metadata.create_all(
            self.store.engine,
            tables=[
                Base.metadata.tables[name]
                for name in INITIAL_TABLES
                if name != "tenants"
            ],
        )

        status = migration_status(self.store.engine)
        self.assertEqual(status.state, "incompatible")
        self.assertIn("add_column", status.schema_error)
        with self.assertRaises(LegacySchemaMismatch):
            self.store.create_schema()
        self.assertNotIn("alembic_version", inspect(self.store.engine).get_table_names())

    def test_schema_drift_at_head_is_detected_and_not_recreated(self) -> None:
        self.store.create_schema()
        with self.store.engine.begin() as connection:
            connection.execute(text("DROP TABLE agent_activations"))

        status = migration_status(self.store.engine)
        self.assertEqual(status.state, "drifted")
        self.assertFalse(status.schema_valid)
        with self.assertRaises(SchemaDriftError):
            self.store.create_schema()
        self.assertNotIn("agent_activations", inspect(self.store.engine).get_table_names())

    def test_revision_chain_has_one_head_and_matches_declared_versions(self) -> None:
        config = Config("alembic.ini")
        scripts = ScriptDirectory.from_config(config)
        revisions = {
            revision.revision: revision.down_revision
            for revision in scripts.walk_revisions()
        }

        self.assertEqual(scripts.get_heads(), [HEAD_REVISION])
        self.assertEqual(
            revisions,
            {
                HEAD_REVISION: OPERATIONS_REVISION,
                OPERATIONS_REVISION: INITIAL_REVISION,
                INITIAL_REVISION: None,
            },
        )
        self.assertEqual(HEAD_TABLES, frozenset(Base.metadata.tables))

    def test_migrations_render_postgresql_sql_with_jsonb_and_version_updates(self) -> None:
        output = io.StringIO()
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.set_main_option(
            "sqlalchemy.url",
            "postgresql+psycopg://migration:unused@localhost/center",
        )
        config.output_buffer = output

        command.upgrade(config, "head", sql=True)

        sql = output.getvalue()
        self.assertIn("CREATE TABLE agent_activations", sql)
        self.assertIn("JSONB", sql)
        self.assertIn(HEAD_REVISION, sql)


class CenterMigrationCommandTests(unittest.TestCase):
    def _config(self) -> SimpleNamespace:
        return SimpleNamespace(
            center_database_url="sqlite+pysqlite:///:memory:",
            center_api_key_pepper="",
        )

    def test_init_db_upgrades_and_prints_current_and_head(self) -> None:
        output = io.StringIO()
        with patch("main.load_config", return_value=self._config()):
            with redirect_stdout(output):
                exit_code = main.run_center_command(
                    "unused.env",
                    Namespace(center_command="init-db"),
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["current"], [HEAD_REVISION])
        self.assertEqual(payload["heads"], [HEAD_REVISION])
        self.assertTrue(payload["at_head"])

    def test_db_status_is_read_only_and_nonzero_for_empty_database(self) -> None:
        output = io.StringIO()
        with patch("main.load_config", return_value=self._config()):
            with redirect_stdout(output):
                exit_code = main.run_center_command(
                    "unused.env",
                    Namespace(center_command="db-status"),
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["state"], "empty")
        self.assertFalse(payload["versioned"])


if __name__ == "__main__":
    unittest.main()
