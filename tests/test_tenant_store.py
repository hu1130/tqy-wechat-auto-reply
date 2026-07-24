from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
import unittest
import uuid

from sqlalchemy import func, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from wechat_ai import tenant_store as tenant_store_module
from wechat_ai.tenant_store import (
    AccessCredential,
    AgentActivation,
    Base,
    ChannelContact,
    ChannelAccount,
    ChatPolicy,
    DeliveryJob,
    FriendRequest,
    Membership,
    Message,
    OperationAttempt,
    OperationJob,
    OperationLimit,
    Tenant,
    TenantStore,
    utc_now,
)


class TenantStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TenantStore("sqlite+pysqlite:///:memory:")
        self.store.create_schema()
        self.tenant_a = self.store.create_tenant("tenant-a", "Tenant A")
        self.tenant_b = self.store.create_tenant("tenant-b", "Tenant B")
        self.account_a = self.store.create_channel_account(
            self.tenant_a.id, "wechat", "wx-account", display_name="A WeChat"
        )
        self.account_b = self.store.create_channel_account(
            self.tenant_b.id, "wechat", "wx-account", display_name="B WeChat"
        )
        self.agent_a = self.store.register_agent(
            self.tenant_a.id, self.account_a.id, "agent-a", status="online"
        )
        self.agent_b = self.store.register_agent(
            self.tenant_b.id, self.account_b.id, "agent-b", status="online"
        )
        self._policy_actors = {}

    def _set_contact_policy(
        self,
        tenant_id: str,
        account_id: str,
        external_contact_id: str,
        mode: str,
        *,
        kind: str = "friend",
        display_name: str = "",
        remark: str = "",
    ):
        actor = self._policy_actors.get(tenant_id)
        if actor is None:
            actor = self._create_actor(tenant_id, f"policy-{len(self._policy_actors)}")
            self._policy_actors[tenant_id] = actor
        contact = self.store.sync_channel_contacts(
            tenant_id,
            account_id,
            [
                {
                    "external_contact_id": external_contact_id,
                    "kind": kind,
                    "display_name": display_name,
                    "remark": remark,
                }
            ],
            utc_now(),
        )[0]
        self.store.set_chat_policy(tenant_id, contact.id, mode, False, actor.id)
        return contact

    def _record_message(
        self,
        tenant_id: str,
        account_id: str,
        suffix: str = "1",
        *,
        auto_reply: bool = True,
    ):
        if auto_reply:
            self._set_contact_policy(
                tenant_id,
                account_id,
                f"customer-{suffix}",
                "ai_auto",
                display_name=f"Customer {suffix}",
            )
        return self.store.record_inbound_message(
            tenant_id,
            account_id,
            f"conversation-{suffix}",
            f"message-{suffix}",
            content=f"hello-{suffix}",
            raw_json={"id": suffix, "nested": {"source": "wechat"}},
            sender_external_id=f"customer-{suffix}",
            customer_display_name=f"Customer {suffix}",
        )

    def _create_actor(self, tenant_id: str, suffix: str):
        user = self.store.create_user(f"operator-{suffix}@example.com")
        self.store.add_membership(tenant_id, user.id, "admin")
        return user

    def _sync_requests(
        self,
        tenant_id: str,
        account_id: str,
        *external_ids: str,
        observed_at=None,
    ):
        seen_at = observed_at or utc_now()
        return self.store.sync_friend_requests(
            tenant_id,
            account_id,
            [
                {
                    "external_request_id": external_id,
                    "requester_external_id": f"wxid-{external_id}",
                    "display_name": f"Name {external_id}",
                    "verification_message": f"Verify {external_id}",
                    "requested_at": seen_at + timedelta(seconds=index),
                    "raw_json": {"request": external_id},
                }
                for index, external_id in enumerate(external_ids)
            ],
            seen_at,
        )

    def test_schema_contains_all_required_tables_and_tenant_columns(self) -> None:
        expected_tables = {
            "tenants",
            "users",
            "memberships",
            "channel_accounts",
            "agent_instances",
            "agent_activations",
            "access_credentials",
            "conversations",
            "messages",
            "reply_jobs",
            "bot_profiles",
            "knowledge_bases",
            "delivery_jobs",
            "api_credentials",
            "tenant_secrets",
            "audit_events",
            "channel_contacts",
            "chat_policies",
            "friend_requests",
            "operation_limits",
            "operation_jobs",
            "operation_attempts",
        }
        inspector = inspect(self.store.engine)

        self.assertEqual(
            set(inspector.get_table_names()),
            expected_tables | {"alembic_version"},
        )
        for table_name in expected_tables - {"tenants", "users"}:
            columns = {column["name"]: column for column in inspector.get_columns(table_name)}
            self.assertIn("tenant_id", columns)
            self.assertFalse(columns["tenant_id"]["nullable"])
        required_unique_constraints = {
            "channel_contacts": "uq_channel_contact_external",
            "chat_policies": "uq_chat_policy_tenant_contact",
            "friend_requests": "uq_friend_request_external",
            "operation_limits": "uq_operation_limit_tenant_channel",
            "operation_jobs": "uq_operation_tenant_idempotency",
            "operation_attempts": "uq_operation_attempt_number",
            "agent_activations": "uq_agent_activation_tenant_idempotency",
        }
        for table_name, constraint_name in required_unique_constraints.items():
            with self.subTest(table=table_name, constraint=constraint_name):
                names = {
                    item["name"]
                    for item in inspector.get_unique_constraints(table_name)
                }
                self.assertIn(constraint_name, names)

    def test_only_postgresql_and_in_memory_sqlite_urls_are_accepted(self) -> None:
        with self.assertRaisesRegex(ValueError, "test backend"):
            TenantStore("sqlite+pysqlite:///production.sqlite3")
        with self.assertRaisesRegex(ValueError, "postgresql\+psycopg"):
            TenantStore("postgresql://user:password@localhost/database")

        postgresql_store = TenantStore(
            "postgresql+psycopg://user:password@localhost/database"
        )
        self.assertEqual(postgresql_store.engine.dialect.name, "postgresql")
        postgresql_store.engine.dispose()

    def test_ids_are_uuid_strings_and_timestamps_are_utc(self) -> None:
        uuid.UUID(self.tenant_a.id)
        uuid.UUID(self.account_a.id)
        self.assertIsNotNone(self.tenant_a.created_at.tzinfo)
        self.assertEqual(self.tenant_a.created_at.utcoffset(), timedelta(0))

        heartbeat = self.store.heartbeat_agent(self.tenant_a.id, self.agent_a.id)
        self.assertEqual(heartbeat.last_heartbeat_at.tzinfo, timezone.utc)

    def test_wechat_identity_is_persistent_confirmed_idempotently_and_rebindable(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "identity")
        self.assertEqual(
            self.store.get_channel_wechat_identity(
                self.tenant_a.id, self.account_a.id
            )["status"],
            "unavailable",
        )
        with self.assertRaisesRegex(ValueError, "not pending confirmation"):
            self.store.confirm_channel_wechat_identity(
                self.tenant_a.id,
                self.account_a.id,
                actor.id,
                "confirm-without-observation",
            )

        account, pending = self.store.observe_channel_wechat_identity(
            self.tenant_a.id,
            self.account_a.id,
            {"account_id": "wxid_account_a", "nickname": "Old Name"},
            "fingerprint-account-a",
        )
        confirmed_account, verified, created = (
            self.store.confirm_channel_wechat_identity(
                self.tenant_a.id,
                self.account_a.id,
                actor.id,
                "confirm-account-a",
            )
        )
        _, repeated, repeated_created = self.store.confirm_channel_wechat_identity(
            self.tenant_a.id,
            self.account_a.id,
            actor.id,
            "confirm-account-a",
        )

        self.assertEqual(pending["status"], "pending")
        self.assertEqual(verified["status"], "verified")
        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(repeated, verified)
        self.assertNotIn("fingerprint-account-a", str(pending))
        self.assertNotIn("fingerprint-account-a", str(verified))
        self.assertIn("_center_wechat_identity_v1", account.config_json)
        self.assertEqual(confirmed_account.id, self.account_a.id)

        _, renamed = self.store.observe_channel_wechat_identity(
            self.tenant_a.id,
            self.account_a.id,
            {"account_id": "wxid_account_a", "nickname": "New Name"},
            "fingerprint-account-a",
        )
        _, mismatch = self.store.observe_channel_wechat_identity(
            self.tenant_a.id,
            self.account_a.id,
            {"account_id": "wxid_account_b", "nickname": "Other Account"},
            "fingerprint-account-b",
        )
        _, rebound, rebound_created = self.store.confirm_channel_wechat_identity(
            self.tenant_a.id,
            self.account_a.id,
            actor.id,
            "confirm-account-b",
        )

        self.assertEqual(renamed["status"], "verified")
        self.assertEqual(renamed["observed_nickname"], "New Name")
        self.assertEqual(mismatch["status"], "mismatch")
        self.assertEqual(mismatch["observed_account_id"], "wxid_account_b")
        self.assertTrue(rebound_created)
        self.assertEqual(rebound["status"], "verified")
        identity_audits = [
            event
            for event in self.store.list_audit_events(self.tenant_a.id)
            if event.action == "channel_account.wechat_identity_confirmed"
        ]
        self.assertEqual(len(identity_audits), 2)
        self.assertNotIn("fingerprint", str(identity_audits))

    def test_wechat_identity_nickname_fallback_and_tenant_boundaries(self) -> None:
        actor_a = self._create_actor(self.tenant_a.id, "identity-a")
        actor_b = self._create_actor(self.tenant_b.id, "identity-b")
        nickname_account = self.store.create_channel_account(
            self.tenant_a.id,
            "wechat",
            "nickname-only-account",
        )
        self.store.observe_channel_wechat_identity(
            self.tenant_a.id,
            nickname_account.id,
            {"nickname": "First Name"},
            "nickname-first",
        )
        self.store.confirm_channel_wechat_identity(
            self.tenant_a.id,
            nickname_account.id,
            actor_a.id,
            "confirm-nickname",
        )
        _, same_name = self.store.observe_channel_wechat_identity(
            self.tenant_a.id,
            nickname_account.id,
            {"nickname": "First Name"},
            "nickname-first",
        )
        _, changed_name = self.store.observe_channel_wechat_identity(
            self.tenant_a.id,
            nickname_account.id,
            {"nickname": "Second Name"},
            "nickname-second",
        )

        self.assertEqual(same_name["status"], "verified")
        self.assertEqual(changed_name["status"], "mismatch")
        with self.assertRaises(LookupError):
            self.store.get_channel_wechat_identity(
                self.tenant_a.id, self.account_b.id
            )
        with self.assertRaises(LookupError):
            self.store.observe_channel_wechat_identity(
                self.tenant_a.id,
                self.account_b.id,
                {"account_id": "cross-tenant"},
                "cross-tenant-fingerprint",
            )
        with self.assertRaisesRegex(ValueError, "not a member"):
            self.store.confirm_channel_wechat_identity(
                self.tenant_a.id,
                nickname_account.id,
                actor_b.id,
                "cross-tenant-actor",
            )

    def test_agent_activation_is_tenant_scoped_bound_and_idempotent(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "activation-owner")
        second_account = self.store.create_channel_account(
            self.tenant_a.id,
            "wechat",
            "wx-account-second",
        )
        first, first_created = self.store.create_agent_activation(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            900,
            "a" * 64,
            "activation-request-1",
            actor.id,
        )
        repeated, repeated_created = self.store.create_agent_activation(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            900,
            "a" * 64,
            "activation-request-1",
            actor.id,
        )

        self.assertTrue(first_created)
        self.assertFalse(repeated_created)
        self.assertEqual(first.id, repeated.id)
        self.assertEqual(first.agent_id, self.agent_a.id)
        self.assertEqual(first.channel_account_id, self.account_a.id)
        with self.assertRaisesRegex(ValueError, "already used"):
            self.store.create_agent_activation(
                self.tenant_a.id,
                self.agent_a.id,
                self.account_a.id,
                901,
                "b" * 64,
                "activation-request-1",
                actor.id,
            )
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.store.create_agent_activation(
                self.tenant_a.id,
                self.agent_a.id,
                second_account.id,
                900,
                "c" * 64,
                "activation-wrong-channel",
                actor.id,
            )
        with self.assertRaises(LookupError):
            self.store.create_agent_activation(
                self.tenant_a.id,
                self.agent_b.id,
                self.account_a.id,
                900,
                "d" * 64,
                "activation-cross-tenant",
                actor.id,
            )

    def test_agent_activation_consumes_once_rotates_token_and_records_device_digest(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "activation-consumer")
        self.store.store_access_credential(
            self.tenant_a.id,
            "agent",
            self.agent_a.id,
            "old-key",
            "agent_token_",
            "old-token-hash",
        )
        activation, _ = self.store.create_agent_activation(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            900,
            "e" * 64,
            "activation-consume-once",
            actor.id,
        )

        consumed, agent = self.store.consume_agent_activation(
            "e" * 64,
            "f" * 64,
            "Reception PC",
            "1.0.0",
            "new-key",
            "agent_token_",
            "new-token-hash",
        )

        self.assertEqual(agent.id, self.agent_a.id)
        self.assertEqual(consumed.id, activation.id)
        self.assertEqual(consumed.consumed_device_hash, "f" * 64)
        self.assertIsNotNone(consumed.consumed_at)
        self.assertIsNone(
            self.store.get_access_credential(
                self.tenant_a.id,
                "old-key",
                "agent_token_",
            )
        )
        self.assertIsNotNone(
            self.store.get_access_credential(
                self.tenant_a.id,
                "new-key",
                "agent_token_",
            )
        )
        for replay_device_hash in ("f" * 64, "0" * 64):
            with self.assertRaisesRegex(ValueError, "activation_failed"):
                self.store.consume_agent_activation(
                    "e" * 64,
                    replay_device_hash,
                    "Another PC",
                    "1.0.1",
                    f"replay-{replay_device_hash[0]}",
                    "agent_token_",
                    "replay-token-hash",
                )

    def test_agent_activation_expiry_and_concurrent_consumption_fail_closed(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "activation-expiry")
        started_at = utc_now()
        self.store.create_agent_activation(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            300,
            "1" * 64,
            "activation-expired",
            actor.id,
            now=started_at,
        )
        with self.assertRaisesRegex(ValueError, "activation_failed"):
            self.store.consume_agent_activation(
                "1" * 64,
                "2" * 64,
                "Expired PC",
                "1.0.0",
                "expired-key",
                "agent_token_",
                "expired-token-hash",
                now=started_at + timedelta(seconds=301),
            )

        self.store.create_agent_activation(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            900,
            "3" * 64,
            "activation-concurrent",
            actor.id,
        )

        def consume(index: int) -> str:
            try:
                self.store.consume_agent_activation(
                    "3" * 64,
                    f"{index}" * 64,
                    f"PC {index}",
                    "1.0.0",
                    f"concurrent-{index}",
                    "agent_token_",
                    f"concurrent-token-hash-{index}",
                )
                return "consumed"
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(consume, (4, 5)))
        self.assertEqual(sorted(results), ["consumed", "rejected"])

    def test_agent_activation_plaintext_code_and_token_are_never_persisted(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "activation-secrets")
        plaintext_code = "wxact_plaintext-must-not-be-stored"
        plaintext_token = "agent_token_plaintext-must-not-be-stored"
        self.store.create_agent_activation(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            900,
            "6" * 64,
            "activation-secret-storage",
            actor.id,
        )
        self.store.consume_agent_activation(
            "6" * 64,
            "7" * 64,
            "Secure PC",
            "1.0.0",
            "secret-key-id",
            "agent_token_",
            "8" * 64,
        )

        with self.store.engine.connect() as connection:
            activation_rows = connection.execute(
                text("SELECT * FROM agent_activations")
            ).mappings().all()
            credential_rows = connection.execute(
                text("SELECT * FROM access_credentials")
            ).mappings().all()
        stored_dump = repr((activation_rows, credential_rows))
        self.assertNotIn(plaintext_code, stored_dump)
        self.assertNotIn(plaintext_token, stored_dump)
        self.assertNotIn("activation_code", {column.name for column in AgentActivation.__table__.columns})
        self.assertNotIn("agent_token", {column.name for column in AccessCredential.__table__.columns})

    def test_cross_tenant_reads_and_writes_are_isolated(self) -> None:
        message_a, _ = self._record_message(self.tenant_a.id, self.account_a.id)
        message_b, _ = self._record_message(self.tenant_b.id, self.account_b.id)

        self.assertEqual([item.id for item in self.store.list_messages(self.tenant_a.id)], [message_a.id])
        self.assertEqual([item.id for item in self.store.list_messages(self.tenant_b.id)], [message_b.id])
        with self.assertRaises(LookupError):
            self.store.list_messages(
                self.tenant_b.id, channel_account_id=self.account_a.id
            )
        with self.assertRaises(LookupError):
            self._record_message(self.tenant_b.id, self.account_a.id, "cross-tenant")
        with self.assertRaises(LookupError):
            self.store.heartbeat_agent(self.tenant_b.id, self.agent_a.id)

    def test_management_reads_are_tenant_scoped(self) -> None:
        shared_user = self.store.create_user(
            "shared@example.com", display_name="Shared User", status="disabled"
        )
        self.store.add_membership(self.tenant_a.id, shared_user.id, "admin")
        self.store.add_membership(self.tenant_b.id, shared_user.id, "viewer")

        tenant = self.store.get_tenant(self.tenant_a.id)
        members_a = self.store.list_tenant_members(self.tenant_a.id)
        members_b = self.store.list_tenant_members(self.tenant_b.id)
        accounts_a = self.store.list_channel_accounts(self.tenant_a.id)
        agents_a = self.store.list_agent_instances(self.tenant_a.id)

        self.assertEqual(tenant.id, self.tenant_a.id)
        self.assertEqual([(item.email, item.role) for item in members_a], [
            ("shared@example.com", "admin")
        ])
        self.assertEqual([(item.email, item.role) for item in members_b], [
            ("shared@example.com", "viewer")
        ])
        self.assertEqual(members_a[0].display_name, "Shared User")
        self.assertEqual(members_a[0].status, "disabled")
        self.assertEqual([item.id for item in accounts_a], [self.account_a.id])
        self.assertEqual([item.id for item in agents_a], [self.agent_a.id])
        with self.assertRaises(LookupError):
            self.store.get_tenant(str(uuid.uuid4()))

    def test_management_lists_have_stable_sorting(self) -> None:
        user_z = self.store.create_user("z@example.com", display_name="Z")
        user_a = self.store.create_user("a@example.com", display_name="A")
        self.store.add_membership(self.tenant_a.id, user_z.id, "viewer")
        self.store.add_membership(self.tenant_a.id, user_a.id, "admin")
        account_z = self.store.create_channel_account(
            self.tenant_a.id, "wechat", "z-account"
        )
        account_a = self.store.create_channel_account(
            self.tenant_a.id, "email", "a-account"
        )
        agent_z = self.store.register_agent(
            self.tenant_a.id, account_z.id, "z-agent"
        )
        agent_a = self.store.register_agent(
            self.tenant_a.id, account_a.id, "a-agent"
        )

        self.assertEqual(
            [item.email for item in self.store.list_tenant_members(self.tenant_a.id)],
            ["a@example.com", "z@example.com"],
        )
        self.assertEqual(
            [item.id for item in self.store.list_channel_accounts(self.tenant_a.id)],
            [account_a.id, self.account_a.id, account_z.id],
        )
        self.assertEqual(
            [item.id for item in self.store.list_agent_instances(self.tenant_a.id)],
            [agent_a.id, self.agent_a.id, agent_z.id],
        )

    def test_management_list_limits_are_bounded(self) -> None:
        list_methods = (
            self.store.list_tenant_members,
            self.store.list_channel_accounts,
            self.store.list_agent_instances,
        )
        for method in list_methods:
            with self.subTest(method=method.__name__, limit=0):
                with self.assertRaisesRegex(ValueError, "between 1 and 500"):
                    method(self.tenant_a.id, limit=0)
            with self.subTest(method=method.__name__, limit=501):
                with self.assertRaisesRegex(ValueError, "between 1 and 500"):
                    method(self.tenant_a.id, limit=501)
            with self.subTest(method=method.__name__, limit="10"):
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    method(self.tenant_a.id, limit="10")

    def test_credentials_secrets_and_audit_are_tenant_scoped(self) -> None:
        credential_a = self.store.store_api_credential(
            self.tenant_a.id,
            "ai-provider",
            "primary",
            "encrypted-a",
            metadata={"key_id": "key-a"},
        )
        credential_b = self.store.store_api_credential(
            self.tenant_b.id, "ai-provider", "primary", "encrypted-b"
        )
        self.store.store_tenant_secret(self.tenant_a.id, "webhook", "secret-a")
        self.store.store_tenant_secret(self.tenant_b.id, "webhook", "secret-b")

        self.assertEqual(
            self.store.get_api_credential(
                self.tenant_a.id, "ai-provider", "primary"
            ).id,
            credential_a.id,
        )
        self.assertEqual(
            self.store.get_api_credential(
                self.tenant_b.id, "ai-provider", "primary"
            ).id,
            credential_b.id,
        )
        self.assertEqual(
            self.store.get_tenant_secret(self.tenant_a.id, "webhook").encrypted_value,
            "secret-a",
        )

        user = self.store.create_user("member@example.com")
        with self.assertRaisesRegex(ValueError, "not a member"):
            self.store.append_audit(
                self.tenant_a.id, "config.updated", actor_user_id=user.id
            )
        self.store.add_membership(self.tenant_a.id, user.id, "admin")
        event = self.store.append_audit(
            self.tenant_a.id,
            "config.updated",
            actor_user_id=user.id,
            entity_type="bot_profile",
            details={"field": "prompt"},
        )
        self.assertEqual(event.tenant_id, self.tenant_a.id)

    def test_duplicate_message_is_idempotent_and_preserves_original_json(self) -> None:
        first, first_created = self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "conversation-1",
            "message-1",
            content="original",
            raw_json={"version": 1, "body": {"text": "original"}},
        )
        duplicate, duplicate_created = self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "changed-conversation",
            "message-1",
            content="changed",
            raw_json={"version": 2},
        )

        self.assertTrue(first_created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.id, first.id)
        self.assertEqual(duplicate.content, "original")
        self.assertEqual(duplicate.raw_json, {"version": 1, "body": {"text": "original"}})
        self.assertEqual(len(self.store.list_messages(self.tenant_a.id)), 1)

    def test_messages_reuse_conversation(self) -> None:
        first, _ = self._record_message(self.tenant_a.id, self.account_a.id, "same")
        second, _ = self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "conversation-same",
            "message-second",
            content="second",
            raw_json={"sequence": 2},
        )
        self.assertEqual(first.conversation_id, second.conversation_id)

    def test_reply_job_retries_and_atomically_creates_delivery(self) -> None:
        start = utc_now()
        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "reply-customer",
            "ai_auto",
            display_name="Reply Customer",
        )
        message, _ = self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "reply-conversation",
            "reply-message",
            content="question",
            raw_json={},
            sender_external_id="reply-customer",
            customer_display_name="Reply Customer",
            received_at=start,
        )
        first_claim = self.store.claim_reply_jobs(
            self.tenant_a.id,
            lease_seconds=60,
            now=start + timedelta(seconds=1),
        )[0]
        context = self.store.get_reply_job_context(
            self.tenant_a.id,
            first_claim.id,
            first_claim.lease_token,
        )
        self.assertEqual(context[1].id, message.id)
        self.assertEqual([item.content for item in context[3]], ["question"])

        retry = self.store.fail_reply_job(
            self.tenant_a.id,
            first_claim.id,
            first_claim.lease_token,
            "temporary AI outage",
            retry_delay_seconds=5,
            now=start + timedelta(seconds=2),
        )
        self.assertEqual(retry.status, "queued")
        self.assertEqual(
            self.store.claim_reply_jobs(
                self.tenant_a.id,
                now=start + timedelta(seconds=6),
            ),
            [],
        )
        second_claim = self.store.claim_reply_jobs(
            self.tenant_a.id,
            now=start + timedelta(seconds=7),
        )[0]
        delivery = self.store.complete_reply_job(
            self.tenant_a.id,
            second_claim.id,
            second_claim.lease_token,
            "generated reply",
            now=start + timedelta(seconds=8),
        )
        repeated = self.store.complete_reply_job(
            self.tenant_a.id,
            second_claim.id,
            second_claim.lease_token,
            "generated reply",
            now=start + timedelta(seconds=9),
        )

        self.assertEqual(delivery.id, repeated.id)
        self.assertEqual(delivery.source_message_id, message.id)
        self.assertEqual(delivery.payload_json["content"], "generated reply")
        self.assertEqual(len(self.store.list_delivery_jobs(self.tenant_a.id)), 1)

    def test_reply_generation_stops_when_contact_policy_changes(self) -> None:
        start = utc_now()
        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "policy-change-before-generation",
            "ai_auto",
        )
        self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "policy-change-conversation",
            "policy-change-message",
            content="question",
            raw_json={},
            sender_external_id="policy-change-before-generation",
            received_at=start,
        )
        reply_job = self.store.claim_reply_jobs(
            self.tenant_a.id,
            now=start + timedelta(seconds=1),
        )[0]
        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "policy-change-before-generation",
            "manual",
        )

        with self.assertRaisesRegex(ValueError, "policy no longer allows"):
            self.store.complete_reply_job(
                self.tenant_a.id,
                reply_job.id,
                reply_job.lease_token,
                "must not be queued",
                now=start + timedelta(seconds=2),
            )
        self.assertEqual(self.store.list_delivery_jobs(self.tenant_a.id), [])

    def test_delivery_start_stops_when_contact_policy_changes(self) -> None:
        start = utc_now()
        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "policy-change-before-send",
            "ai_auto",
        )
        self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "policy-send-conversation",
            "policy-send-message",
            content="question",
            raw_json={},
            sender_external_id="policy-change-before-send",
            received_at=start,
        )
        reply_job = self.store.claim_reply_jobs(
            self.tenant_a.id,
            now=start + timedelta(seconds=1),
        )[0]
        delivery = self.store.complete_reply_job(
            self.tenant_a.id,
            reply_job.id,
            reply_job.lease_token,
            "generated reply",
            now=start + timedelta(seconds=2),
        )
        claimed = self.store.claim_deliveries(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            now=start + timedelta(seconds=3),
        )[0]
        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "policy-change-before-send",
            "blocked",
        )

        suppressed = self.store.start_delivery(
            self.tenant_a.id,
            delivery.id,
            self.agent_a.id,
            claimed.lease_token,
            claimed.attempts,
            now=start + timedelta(seconds=4),
        )
        repeated = self.store.start_delivery(
            self.tenant_a.id,
            delivery.id,
            self.agent_a.id,
            claimed.lease_token,
            claimed.attempts,
            now=start + timedelta(seconds=5),
        )

        self.assertEqual(suppressed.status, "failed")
        self.assertEqual(suppressed.error, "reply_policy_not_ai_auto")
        self.assertEqual(repeated.status, "failed")

    def test_enqueue_delivery_is_idempotent_per_tenant(self) -> None:
        first = self.store.enqueue_delivery(
            self.tenant_a.id, self.account_a.id, "reply-1", {"text": "first"}
        )
        duplicate = self.store.enqueue_delivery(
            self.tenant_a.id, self.account_a.id, "reply-1", {"text": "changed"}
        )
        other_tenant = self.store.enqueue_delivery(
            self.tenant_b.id, self.account_b.id, "reply-1", {"text": "tenant-b"}
        )

        self.assertEqual(duplicate.id, first.id)
        self.assertEqual(duplicate.payload_json, {"text": "first"})
        self.assertNotEqual(other_tenant.id, first.id)

    def test_concurrent_double_claim_uses_sqlite_status_cas(self) -> None:
        second_agent = self.store.register_agent(
            self.tenant_a.id, self.account_a.id, "agent-a-2", status="online"
        )
        job = self.store.enqueue_delivery(
            self.tenant_a.id, self.account_a.id, "claim-once", {"text": "reply"}
        )

        def claim(agent_id: str):
            return self.store.claim_deliveries(
                self.tenant_a.id,
                agent_id,
                self.account_a.id,
                limit=1,
                lease_seconds=30,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(claim, self.agent_a.id)
            second_future = executor.submit(claim, second_agent.id)
            claims = first_future.result() + second_future.result()

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].id, job.id)
        self.assertEqual(claims[0].attempts, 1)
        self.assertIsNotNone(claims[0].lease_token)

    def test_expired_delivery_is_reclaimed_and_old_lease_cannot_complete(self) -> None:
        second_agent = self.store.register_agent(
            self.tenant_a.id, self.account_a.id, "agent-a-2", status="online"
        )
        start = utc_now()
        job = self.store.enqueue_delivery(
            self.tenant_a.id,
            self.account_a.id,
            "reclaim",
            {"text": "reply"},
            available_at=start,
        )
        first_claim = self.store.claim_deliveries(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            lease_seconds=5,
            now=start,
        )[0]
        second_claim = self.store.claim_deliveries(
            self.tenant_a.id,
            second_agent.id,
            self.account_a.id,
            lease_seconds=30,
            now=start + timedelta(seconds=6),
        )[0]

        self.assertEqual(second_claim.id, job.id)
        self.assertEqual(second_claim.attempts, 2)
        self.assertNotEqual(second_claim.lease_token, first_claim.lease_token)
        with self.assertRaisesRegex(ValueError, "another agent"):
            self.store.complete_delivery(
                self.tenant_a.id,
                job.id,
                self.agent_a.id,
                first_claim.lease_token,
                now=start + timedelta(seconds=7),
            )
        self.store.start_delivery(
            self.tenant_a.id,
            job.id,
            second_agent.id,
            second_claim.lease_token,
            second_claim.attempts,
            now=start + timedelta(seconds=7),
        )
        completed = self.store.complete_delivery(
            self.tenant_a.id,
            job.id,
            second_agent.id,
            second_claim.lease_token,
            now=start + timedelta(seconds=7),
        )
        self.assertEqual(completed.status, "completed")

    def test_sending_is_not_reclaimed_and_uncertain_is_terminal(self) -> None:
        start = utc_now()
        job = self.store.enqueue_delivery(
            self.tenant_a.id,
            self.account_a.id,
            "uncertain-delivery",
            {"content": "reply", "conversation_external_id": "customer"},
            available_at=start,
        )
        claim = self.store.claim_deliveries(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            lease_seconds=5,
            now=start,
        )[0]
        sending = self.store.start_delivery(
            self.tenant_a.id,
            job.id,
            self.agent_a.id,
            claim.lease_token,
            claim.attempts,
            now=start + timedelta(seconds=1),
        )

        self.assertEqual(sending.status, "sending")
        self.assertEqual(
            self.store.claim_deliveries(
                self.tenant_a.id,
                self.agent_a.id,
                self.account_a.id,
                now=start + timedelta(minutes=10),
            ),
            [],
        )
        uncertain = self.store.complete_delivery(
            self.tenant_a.id,
            job.id,
            self.agent_a.id,
            claim.lease_token,
            claim.attempts,
            result_status="uncertain",
            error="result unknown after Agent restart",
            now=start + timedelta(minutes=10),
        )
        self.assertEqual(uncertain.status, "uncertain")
        owner = self.store.create_user("resolver@example.com")
        self.store.add_membership(self.tenant_a.id, owner.id, "owner")
        resolved = self.store.resolve_uncertain_delivery(
            self.tenant_a.id,
            job.id,
            "retry",
            "resolve-1",
            actor_user_id=owner.id,
            note="Operator verified the message was not sent",
        )
        repeated = self.store.resolve_uncertain_delivery(
            self.tenant_a.id,
            job.id,
            "retry",
            "resolve-1",
            actor_user_id=owner.id,
            note="Operator verified the message was not sent",
        )
        self.assertEqual(resolved.status, "queued")
        self.assertEqual(repeated.id, job.id)
        self.assertEqual(
            self.store.list_audit_events(self.tenant_a.id)[0].action,
            "delivery.uncertain_resolved",
        )

    def test_access_credentials_require_subject_in_same_tenant(self) -> None:
        user = self.store.create_user("owner@example.com")
        self.store.add_membership(self.tenant_a.id, user.id, "owner")
        credential = self.store.store_access_credential(
            self.tenant_a.id,
            "user",
            user.id,
            "key-1",
            "tenant_api_",
            "hash-1",
        )

        loaded = self.store.get_access_credential(
            self.tenant_a.id,
            "key-1",
            "tenant_api_",
        )
        self.assertEqual(loaded.id, credential.id)
        self.assertIsNone(
            self.store.get_access_credential(
                self.tenant_b.id,
                "key-1",
                "tenant_api_",
            )
        )
        with self.assertRaisesRegex(ValueError, "not a member"):
            self.store.store_access_credential(
                self.tenant_b.id,
                "user",
                user.id,
                "key-2",
                "tenant_api_",
                "hash-2",
            )

    def test_bootstrap_bundle_rolls_back_all_records_on_failure(self) -> None:
        with self.store.engine.connect() as connection:
            before = connection.scalar(select(func.count()).select_from(Tenant))

        with self.assertRaises(IntegrityError):
            self.store.bootstrap_tenant_bundle(
                tenant_slug="tenant-a",
                tenant_name="Rolled Back",
                owner_email="new-owner@example.com",
                channel="wechat_desktop",
                external_account_id="rolled-back-wechat",
                agent_external_id="rolled-back-agent",
                agent_name="",
                tenant_key_id="tenant-key",
                tenant_key_prefix="tenant_api_",
                tenant_key_hash="tenant-hash",
                agent_key_id="agent-key",
                agent_key_prefix="agent_token_",
                agent_key_hash="agent-hash",
            )

        with self.store.engine.connect() as connection:
            after = connection.scalar(select(func.count()).select_from(Tenant))
        self.assertEqual(after, before)

    def test_bootstrap_bundle_reuses_global_user_for_another_tenant(self) -> None:
        first = self.store.bootstrap_tenant_bundle(
            tenant_slug="owner-first",
            tenant_name="Owner First",
            owner_email="shared-owner@example.com",
            channel="wechat_desktop",
            external_account_id="owner-first-wechat",
            agent_external_id="owner-first-agent",
            agent_name="",
            tenant_key_id="tenant-key-first",
            tenant_key_prefix="tenant_api_",
            tenant_key_hash="tenant-hash-first",
            agent_key_id="agent-key-first",
            agent_key_prefix="agent_token_",
            agent_key_hash="agent-hash-first",
        )
        second = self.store.bootstrap_tenant_bundle(
            tenant_slug="owner-second",
            tenant_name="Owner Second",
            owner_email="shared-owner@example.com",
            channel="wechat_desktop",
            external_account_id="owner-second-wechat",
            agent_external_id="owner-second-agent",
            agent_name="",
            tenant_key_id="tenant-key-second",
            tenant_key_prefix="tenant_api_",
            tenant_key_hash="tenant-hash-second",
            agent_key_id="agent-key-second",
            agent_key_prefix="agent_token_",
            agent_key_hash="agent-hash-second",
        )

        self.assertEqual(first[1].id, second[1].id)
        self.assertEqual(
            self.store.get_membership(first[0].id, first[1].id).role,
            "owner",
        )
        self.assertEqual(
            self.store.get_membership(second[0].id, second[1].id).role,
            "owner",
        )

    def test_wrong_agent_cannot_claim_or_complete(self) -> None:
        second_account = self.store.create_channel_account(
            self.tenant_a.id, "wechat", "second-account"
        )
        wrong_channel_agent = self.store.register_agent(
            self.tenant_a.id, second_account.id, "wrong-channel-agent"
        )
        job = self.store.enqueue_delivery(
            self.tenant_a.id, self.account_a.id, "wrong-agent", {"text": "reply"}
        )

        with self.assertRaisesRegex(ValueError, "not bound"):
            self.store.claim_deliveries(
                self.tenant_a.id,
                wrong_channel_agent.id,
                self.account_a.id,
            )
        with self.assertRaises(LookupError):
            self.store.claim_deliveries(
                self.tenant_a.id,
                self.agent_b.id,
                self.account_a.id,
            )

        claim = self.store.claim_deliveries(
            self.tenant_a.id, self.agent_a.id, self.account_a.id
        )[0]
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.store.complete_delivery(
                self.tenant_a.id,
                job.id,
                wrong_channel_agent.id,
                claim.lease_token,
            )
        with self.assertRaises(LookupError):
            self.store.complete_delivery(
                self.tenant_b.id,
                job.id,
                self.agent_a.id,
                claim.lease_token,
            )

    def test_contact_sync_listing_policy_and_tenant_isolation(self) -> None:
        observed = utc_now()
        contacts = self.store.sync_channel_contacts(
            self.tenant_a.id,
            self.account_a.id,
            [
                {
                    "external_contact_id": "wx-z",
                    "kind": "friend",
                    "display_name": "Zulu",
                    "remark": "VIP",
                    "metadata": {"source": "wechat"},
                },
                {
                    "external_contact_id": "group-a",
                    "kind": "group",
                    "display_name": "Alpha",
                },
            ],
            observed,
        )
        self.assertEqual([item.display_name for item in contacts], ["Alpha", "Zulu"])
        updated = self.store.sync_channel_contacts(
            self.tenant_a.id,
            self.account_a.id,
            [
                {
                    "external_contact_id": "wx-z",
                    "kind": "friend",
                    "display_name": "Beta",
                    "status": "inactive",
                }
            ],
            observed + timedelta(minutes=1),
        )[0]
        self.assertEqual(updated.id, contacts[1].id)
        self.assertEqual(updated.metadata_json, {"source": "wechat"})
        self.assertEqual(
            [item.external_contact_id for item in self.store.list_channel_contacts_items(
                self.tenant_a.id, query="vip"
            )],
            ["wx-z"],
        )
        self.assertEqual(
            [item.external_contact_id for item in self.store.list_channel_contacts_items(
                self.tenant_a.id, kind="group", status="active"
            )],
            ["group-a"],
        )

        actor = self._create_actor(self.tenant_a.id, "contact-a")
        policy = self.store.set_chat_policy(
            self.tenant_a.id, contacts[0].id, "ai_auto", True, actor.id
        )
        self.assertEqual((policy.mode, policy.welcome_enabled), ("ai_auto", True))
        policies = self.store.batch_set_chat_policies(
            self.tenant_a.id,
            [contacts[1].id, contacts[0].id, contacts[1].id],
            "paused",
            None,
            actor.id,
        )
        self.assertEqual([item.contact_id for item in policies], [contacts[1].id, contacts[0].id])
        self.assertEqual(
            {item.contact_id: item.welcome_enabled for item in policies},
            {contacts[1].id: False, contacts[0].id: True},
        )
        explicitly_enabled = self.store.batch_set_chat_policies(
            self.tenant_a.id, [contacts[1].id], "manual", True, actor.id
        )[0]
        self.assertTrue(explicitly_enabled.welcome_enabled)
        listed_policies = self.store.list_chat_policies(
            self.tenant_a.id, [contacts[0].id, contacts[1].id]
        )
        self.assertEqual({item.contact_id for item in listed_policies}, {item.id for item in contacts})
        actor_b = self._create_actor(self.tenant_b.id, "contact-b")
        with self.assertRaises(LookupError):
            self.store.set_chat_policy(
                self.tenant_b.id, contacts[0].id, "manual", False, actor_b.id
            )
        with self.assertRaises(LookupError):
            self.store.list_channel_contacts_items(
                self.tenant_b.id, channel_account_id=self.account_a.id
            )
        self.assertEqual(
            self.store.list_chat_policies(self.tenant_b.id, [contacts[0].id]),
            [],
        )
        with self.assertRaisesRegex(ValueError, "more than 500"):
            self.store.list_chat_policies(
                self.tenant_a.id, [str(index) for index in range(501)]
            )
        actions = [item.action for item in self.store.list_audit_events(self.tenant_a.id)]
        self.assertIn("contacts.synced", actions)
        self.assertIn("chat_policy.batch_updated", actions)

    def test_friend_request_sync_is_idempotent_and_preserves_terminal_state(self) -> None:
        observed = utc_now()
        request = self._sync_requests(
            self.tenant_a.id, self.account_a.id, "request-1", observed_at=observed
        )[0]
        approved = self.store.sync_friend_requests(
            self.tenant_a.id,
            self.account_a.id,
            [
                {
                    "external_request_id": "request-1",
                    "requester_external_id": "wxid-request-1",
                    "display_name": "Renamed",
                    "status": "approved",
                }
            ],
            observed + timedelta(minutes=1),
        )[0]
        replayed = self.store.sync_friend_requests(
            self.tenant_a.id,
            self.account_a.id,
            [
                {
                    "external_request_id": "request-1",
                    "requester_external_id": "wxid-request-1",
                    "status": "pending",
                }
            ],
            observed + timedelta(minutes=2),
        )[0]
        self.assertEqual(request.id, approved.id)
        self.assertEqual(replayed.status, "approved")
        self.assertEqual(
            [item.id for item in self.store.list_friend_requests(
                self.tenant_a.id, status="approved", query="wxid-request"
            )],
            [request.id],
        )
        with self.assertRaises(LookupError):
            self.store.list_friend_requests(
                self.tenant_b.id, channel_account_id=self.account_a.id
            )

    def test_inbound_messages_only_enqueue_for_ai_auto_policy(self) -> None:
        modes = ("ai_auto", "manual", "paused", "blocked")
        recorded = {}
        for mode in modes:
            external_id = f"policy-{mode}"
            self._set_contact_policy(
                self.tenant_a.id,
                self.account_a.id,
                external_id,
                mode,
                display_name=mode,
            )
            recorded[mode], _ = self.store.record_inbound_message(
                self.tenant_a.id,
                self.account_a.id,
                f"conversation-{mode}",
                f"message-{mode}",
                content=mode,
                raw_json={},
                sender_external_id=external_id,
                customer_display_name=mode,
            )

        self.store.sync_channel_contacts(
            self.tenant_a.id,
            self.account_a.id,
            [
                {
                    "external_contact_id": "policy-none",
                    "kind": "friend",
                    "display_name": "No Policy",
                }
            ],
            utc_now(),
        )
        no_policy_message, _ = self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "conversation-none",
            "message-none",
            content="no policy",
            raw_json={},
            sender_external_id="policy-none",
            customer_display_name="No Policy",
        )
        jobs = self.store.list_reply_jobs(self.tenant_a.id)
        self.assertEqual(len(self.store.list_messages(self.tenant_a.id)), 5)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source_message_id, recorded["ai_auto"].id)
        self.assertNotEqual(jobs[0].source_message_id, no_policy_message.id)

        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "tenant-shared-contact",
            "ai_auto",
        )
        tenant_b_message, _ = self.store.record_inbound_message(
            self.tenant_b.id,
            self.account_b.id,
            "tenant-shared-conversation",
            "tenant-b-policy-message",
            content="must stay tenant B",
            raw_json={},
            sender_external_id="tenant-shared-contact",
        )
        self.assertEqual(self.store.list_reply_jobs(self.tenant_b.id), [])
        self.assertEqual(
            self.store.list_messages(self.tenant_b.id)[0].id,
            tenant_b_message.id,
        )

    def test_inbound_contact_matching_is_unique_and_group_aware(self) -> None:
        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "duplicate-1",
            "ai_auto",
            display_name="Duplicate Name",
        )
        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "duplicate-2",
            "ai_auto",
            remark="Duplicate Name",
        )
        self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "unknown-duplicate-session",
            "ambiguous-name-message",
            content="ambiguous",
            raw_json={},
            sender_external_id="unknown-sender",
            customer_display_name="Duplicate Name",
        )
        self.assertEqual(self.store.list_reply_jobs(self.tenant_a.id), [])

        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "unique-name-contact",
            "ai_auto",
            display_name="Unique Visible Name",
        )
        unique_message, _ = self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "unknown-unique-session",
            "unique-name-message",
            content="unique fallback",
            raw_json={},
            sender_external_id="unknown-unique-sender",
            customer_display_name="Unique Visible Name",
        )

        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "group-conversation-id",
            "ai_auto",
            kind="group",
            display_name="Operations Group",
        )
        group_message, _ = self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "group-conversation-id",
            "group-message",
            content="group inbound",
            raw_json={},
            sender_external_id="group-member-id",
            customer_display_name="Group Member",
        )

        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "priority-sender",
            "manual",
            display_name="Priority Sender",
        )
        self._set_contact_policy(
            self.tenant_a.id,
            self.account_a.id,
            "priority-conversation",
            "ai_auto",
            display_name="Priority Conversation",
        )
        self.store.record_inbound_message(
            self.tenant_a.id,
            self.account_a.id,
            "priority-conversation",
            "priority-message",
            content="sender policy wins",
            raw_json={},
            sender_external_id="priority-sender",
            customer_display_name="Priority Sender",
        )
        source_ids = {
            item.source_message_id for item in self.store.list_reply_jobs(self.tenant_a.id)
        }
        self.assertEqual(source_ids, {unique_message.id, group_message.id})

    def test_operation_limits_defaults_validation_pause_and_audit(self) -> None:
        defaults = self.store.get_operation_limit(self.tenant_a.id, self.account_a.id)
        self.assertEqual(
            (
                defaults.batch_size,
                defaults.min_interval_seconds,
                defaults.max_per_hour,
                defaults.max_per_day,
                defaults.paused,
                defaults.welcome_message,
                defaults.auto_enable_reply,
            ),
            (10, 5, 30, 100, False, "", False),
        )
        actor = self._create_actor(self.tenant_a.id, "limit-a")
        updated = self.store.update_operation_limit(
            self.tenant_a.id,
            self.account_a.id,
            batch_size=3,
            min_interval_seconds=7,
            max_per_hour=12,
            max_per_day=40,
            paused=True,
            welcome_message="Welcome by default",
            auto_enable_reply=True,
            actor_user_id=actor.id,
        )
        self.assertEqual(
            (
                updated.batch_size,
                updated.paused,
                updated.welcome_message,
                updated.auto_enable_reply,
            ),
            (3, True, "Welcome by default", True),
        )
        for field, value in (
            ("batch_size", 0),
            ("batch_size", 101),
            ("min_interval_seconds", 0),
            ("max_per_hour", 10001),
            ("max_per_day", 100001),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    self.store.update_operation_limit(
                        self.tenant_a.id,
                        self.account_a.id,
                        actor_user_id=actor.id,
                        **{field: value},
                    )
        self.assertEqual(
            self.store.list_audit_events(self.tenant_a.id)[0].action,
            "operation_limit.updated",
        )
        self.assertEqual(
            self.store.list_audit_events(self.tenant_a.id)[0].details_json[
                "welcome_message"
            ],
            "Welcome by default",
        )
        request = self._sync_requests(
            self.tenant_a.id, self.account_a.id, "defaults"
        )[0]
        defaulted_job = self.store.enqueue_friend_request_operations(
            self.tenant_a.id,
            self.account_a.id,
            request_ids=[request.id],
            selection="selected",
            action="accept_friend",
            actor_user_id=actor.id,
            idempotency_key="defaults-command",
        )[0]
        self.assertEqual(
            (
                defaulted_job.payload_json["welcome_message"],
                defaulted_job.payload_json["auto_enable_reply"],
            ),
            ("Welcome by default", True),
        )

    def test_enqueue_selections_are_stable_idempotent_and_conflict_safe(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "enqueue-a")
        requests = self._sync_requests(
            self.tenant_a.id, self.account_a.id, "one", "two", "three"
        )
        self.store.update_operation_limit(
            self.tenant_a.id,
            self.account_a.id,
            batch_size=2,
            actor_user_id=actor.id,
        )
        selected = self.store.enqueue_friend_request_operations(
            self.tenant_a.id,
            self.account_a.id,
            request_ids=[requests[2].id, requests[0].id],
            selection="selected",
            action="accept_friend",
            actor_user_id=actor.id,
            idempotency_key="selected-command",
            welcome_message="Welcome",
            auto_enable_reply=True,
        )
        replay = self.store.enqueue_friend_request_operations(
            self.tenant_a.id,
            self.account_a.id,
            request_ids=[requests[2].id, requests[0].id],
            selection="selected",
            action="accept_friend",
            actor_user_id=actor.id,
            idempotency_key="selected-command",
            welcome_message="Welcome",
            auto_enable_reply=True,
        )
        self.assertEqual([item.id for item in replay], [item.id for item in selected])
        batch = self.store.enqueue_friend_request_operations(
            self.tenant_a.id,
            self.account_a.id,
            selection="batch",
            action="accept_friend",
            actor_user_id=actor.id,
            idempotency_key="batch-command",
        )
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0].friend_request_id, requests[0].id)
        all_jobs = self.store.enqueue_friend_request_operations(
            self.tenant_a.id,
            self.account_a.id,
            selection="all",
            action="accept_friend",
            actor_user_id=actor.id,
            idempotency_key="all-command",
        )
        self.assertEqual(
            [item.friend_request_id for item in all_jobs],
            [item.id for item in requests],
        )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.store.enqueue_friend_request_operations(
                self.tenant_a.id,
                self.account_a.id,
                request_ids=[requests[0].id],
                selection="selected",
                action="reject_friend",
                actor_user_id=actor.id,
                idempotency_key="conflicting-command",
            )
        with self.assertRaises(LookupError):
            self.store.enqueue_friend_request_operations(
                self.tenant_b.id,
                self.account_a.id,
                request_ids=[requests[0].id],
                selection="selected",
                action="accept_friend",
                actor_user_id=actor.id,
                idempotency_key="cross-tenant",
            )

    def test_operation_success_creates_contact_policy_and_dependent_welcome(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "success-a")
        request = self._sync_requests(
            self.tenant_a.id, self.account_a.id, "success"
        )[0]
        job = self.store.enqueue_friend_request_operations(
            self.tenant_a.id,
            self.account_a.id,
            request_ids=[request.id],
            selection="selected",
            action="accept_friend",
            actor_user_id=actor.id,
            idempotency_key="success-command",
            welcome_message="Hello there",
            auto_enable_reply=True,
        )[0]
        claim_at = utc_now() + timedelta(seconds=1)
        claimed = self.store.claim_operation_jobs(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            now=claim_at,
        )[0]
        self.assertEqual(claimed.id, job.id)
        started = self.store.start_operation_job(
            self.tenant_a.id,
            job.id,
            self.agent_a.id,
            claimed.lease_token,
            claimed.attempts,
            now=claim_at + timedelta(milliseconds=100),
        )
        completed = self.store.complete_operation_job(
            self.tenant_a.id,
            job.id,
            self.agent_a.id,
            claimed.lease_token,
            claimed.attempts,
            "succeeded",
            result={"wechat": "accepted"},
            now=claim_at + timedelta(milliseconds=200),
        )
        self.assertEqual((started.status, completed.status), ("executing", "succeeded"))
        refreshed_request = self.store.list_friend_requests(
            self.tenant_a.id, status="approved"
        )[0]
        self.assertEqual(refreshed_request.id, request.id)
        contact = self.store.list_channel_contacts_items(
            self.tenant_a.id, query="wxid-success"
        )[0]
        with self.store._read_session() as session:
            policy = session.scalar(
                select(ChatPolicy).where(ChatPolicy.contact_id == contact.id)
            )
        self.assertEqual((policy.mode, policy.welcome_enabled), ("ai_auto", True))
        welcome = self.store.list_operation_jobs(
            self.tenant_a.id, operation_type="send_welcome"
        )[0]
        self.assertEqual(welcome.depends_on_job_id, job.id)
        self.assertEqual(
            welcome.payload_json,
            {
                "external_contact_id": "wxid-success",
                "conversation_external_id": "Name success",
                "content": "Hello there",
            },
        )
        welcome_claim = self.store.claim_operation_jobs(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            now=claim_at + timedelta(seconds=6),
        )
        self.assertEqual([item.id for item in welcome_claim], [welcome.id])

    def test_welcome_enqueue_rejects_missing_visible_conversation_name(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "missing-name")
        request = self.store.sync_friend_requests(
            self.tenant_a.id,
            self.account_a.id,
            [
                {
                    "external_request_id": "missing-name",
                    "requester_external_id": "wxid-missing-name",
                    "display_name": "",
                }
            ],
            utc_now(),
        )[0]
        with self.assertRaisesRegex(ValueError, "conversation_external_id"):
            self.store.enqueue_friend_request_operations(
                self.tenant_a.id,
                self.account_a.id,
                request_ids=[request.id],
                selection="selected",
                action="accept_friend",
                actor_user_id=actor.id,
                idempotency_key="missing-name-command",
                welcome_message="Hello",
            )

    def test_new_operation_models_are_exported(self) -> None:
        for model_name in (
            "ChannelContact",
            "ChatPolicy",
            "FriendRequest",
            "OperationLimit",
            "OperationJob",
            "OperationAttempt",
        ):
            self.assertIn(model_name, tenant_store_module.__all__)

    def test_operation_rate_limits_pause_and_concurrent_claim(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "rate-a")
        requests = self._sync_requests(
            self.tenant_a.id, self.account_a.id, "rate-1", "rate-2", "rate-3"
        )
        self.store.enqueue_friend_request_operations(
            self.tenant_a.id,
            self.account_a.id,
            request_ids=[item.id for item in requests],
            selection="selected",
            action="reject_friend",
            actor_user_id=actor.id,
            idempotency_key="rate-command",
        )
        self.store.update_operation_limit(
            self.tenant_a.id,
            self.account_a.id,
            min_interval_seconds=5,
            max_per_hour=2,
            max_per_day=2,
            actor_user_id=actor.id,
        )
        claim_at = utc_now() + timedelta(seconds=1)

        def claim_once():
            return self.store.claim_operation_jobs(
                self.tenant_a.id,
                self.agent_a.id,
                self.account_a.id,
                now=claim_at,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim_once(), range(2)))
        first_claims = [job for batch in results for job in batch]
        self.assertEqual(len(first_claims), 1)
        first = first_claims[0]
        self.store.start_operation_job(
            self.tenant_a.id,
            first.id,
            self.agent_a.id,
            first.lease_token,
            first.attempts,
            now=claim_at + timedelta(milliseconds=100),
        )
        self.store.complete_operation_job(
            self.tenant_a.id,
            first.id,
            self.agent_a.id,
            first.lease_token,
            first.attempts,
            "succeeded",
            now=claim_at + timedelta(milliseconds=200),
        )
        self.assertEqual(claim_once(), [])
        second = self.store.claim_operation_jobs(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            now=claim_at + timedelta(seconds=5),
        )
        self.assertEqual(len(second), 1)
        self.assertEqual(
            self.store.claim_operation_jobs(
                self.tenant_a.id,
                self.agent_a.id,
                self.account_a.id,
                now=claim_at + timedelta(seconds=10),
            ),
            [],
        )
        self.store.update_operation_limit(
            self.tenant_a.id,
            self.account_a.id,
            max_per_hour=10,
            max_per_day=2,
            actor_user_id=actor.id,
        )
        self.assertEqual(
            self.store.claim_operation_jobs(
                self.tenant_a.id,
                self.agent_a.id,
                self.account_a.id,
                now=claim_at + timedelta(seconds=10),
            ),
            [],
        )
        self.store.update_operation_limit(
            self.tenant_a.id,
            self.account_a.id,
            paused=True,
            actor_user_id=actor.id,
        )
        self.assertEqual(
            self.store.claim_operation_jobs(
                self.tenant_a.id,
                self.agent_a.id,
                self.account_a.id,
                now=claim_at + timedelta(days=1),
            ),
            [],
        )

    def test_expired_operation_leases_retry_and_uncertain_resolution(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "lease-a")
        requests = self._sync_requests(
            self.tenant_a.id, self.account_a.id, "lease-1"
        )
        jobs = self.store.enqueue_friend_request_operations(
            self.tenant_a.id,
            self.account_a.id,
            request_ids=[item.id for item in requests],
            selection="selected",
            action="reject_friend",
            actor_user_id=actor.id,
            idempotency_key="lease-command",
        )
        self.store.update_operation_limit(
            self.tenant_a.id,
            self.account_a.id,
            min_interval_seconds=1,
            actor_user_id=actor.id,
        )
        base = utc_now() + timedelta(seconds=1)
        claimed = self.store.claim_operation_jobs(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            lease_seconds=1,
            now=base,
        )[0]
        old_token = claimed.lease_token
        reclaimed = self.store.claim_operation_jobs(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            lease_seconds=1,
            now=base + timedelta(seconds=2),
        )[0]
        self.assertEqual(reclaimed.id, jobs[0].id)
        self.assertEqual(reclaimed.attempts, 2)
        with self.assertRaises(ValueError):
            self.store.start_operation_job(
                self.tenant_a.id,
                reclaimed.id,
                self.agent_a.id,
                old_token,
                1,
                now=base + timedelta(seconds=2),
            )
        self.store.start_operation_job(
            self.tenant_a.id,
            reclaimed.id,
            self.agent_a.id,
            reclaimed.lease_token,
            reclaimed.attempts,
            now=base + timedelta(seconds=2, milliseconds=100),
        )
        self.store.claim_operation_jobs(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            now=base + timedelta(seconds=4),
        )
        uncertain = self.store.list_operation_jobs(
            self.tenant_a.id, status="uncertain"
        )[0]
        self.assertEqual(uncertain.id, reclaimed.id)
        resolved = self.store.resolve_operation_job(
            self.tenant_a.id,
            uncertain.id,
            "retry",
            "resolution-1",
            actor.id,
            "Operator checked WeChat",
            now=base + timedelta(seconds=5),
        )
        replayed = self.store.resolve_operation_job(
            self.tenant_a.id,
            uncertain.id,
            "retry",
            "resolution-1",
            actor.id,
            "Operator checked WeChat",
            now=base + timedelta(seconds=5),
        )
        self.assertEqual((resolved.status, replayed.id), ("queued", resolved.id))

    def test_failed_retry_cancel_and_cross_tenant_state_changes(self) -> None:
        actor_a = self._create_actor(self.tenant_a.id, "states-a")
        actor_b = self._create_actor(self.tenant_b.id, "states-b")
        requests = self._sync_requests(
            self.tenant_a.id, self.account_a.id, "cancel", "fail"
        )
        jobs = self.store.enqueue_friend_request_operations(
            self.tenant_a.id,
            self.account_a.id,
            request_ids=[item.id for item in requests],
            selection="selected",
            action="reject_friend",
            actor_user_id=actor_a.id,
            idempotency_key="states-command",
        )
        cancelled = self.store.cancel_operation_job(
            self.tenant_a.id, jobs[0].id, actor_a.id
        )
        self.assertEqual(cancelled.status, "cancelled")
        retried_cancelled = self.store.retry_operation_job(
            self.tenant_a.id, cancelled.id, actor_a.id
        )
        self.assertEqual(
            (retried_cancelled.id, retried_cancelled.status, retried_cancelled.attempts),
            (cancelled.id, "queued", 0),
        )
        with self.assertRaisesRegex(ValueError, "failed or cancelled"):
            self.store.retry_operation_job(
                self.tenant_a.id, retried_cancelled.id, actor_a.id
            )
        with self.assertRaises(LookupError):
            self.store.cancel_operation_job(self.tenant_b.id, jobs[1].id, actor_b.id)
        claim_at = utc_now() + timedelta(seconds=1)
        claimed = self.store.claim_operation_jobs(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            now=claim_at,
        )[0]
        self.store.start_operation_job(
            self.tenant_a.id,
            claimed.id,
            self.agent_a.id,
            claimed.lease_token,
            claimed.attempts,
            now=claim_at + timedelta(milliseconds=100),
        )
        failed = self.store.complete_operation_job(
            self.tenant_a.id,
            claimed.id,
            self.agent_a.id,
            claimed.lease_token,
            claimed.attempts,
            "failed",
            error="button not found",
            now=claim_at + timedelta(milliseconds=200),
        )
        self.assertEqual(failed.status, "failed")
        retried = self.store.retry_operation_job(
            self.tenant_a.id, failed.id, actor_a.id
        )
        self.assertEqual((retried.status, retried.attempts), ("queued", 1))
        with self.assertRaises(LookupError):
            self.store.retry_operation_job(self.tenant_b.id, failed.id, actor_b.id)

    def test_late_persisted_result_resolves_expired_executing_operation(self) -> None:
        actor = self._create_actor(self.tenant_a.id, "late-result")
        request = self._sync_requests(
            self.tenant_a.id, self.account_a.id, "late-result"
        )[0]
        job = self.store.enqueue_friend_request_operations(
            self.tenant_a.id,
            self.account_a.id,
            request_ids=[request.id],
            selection="selected",
            action="reject_friend",
            actor_user_id=actor.id,
            idempotency_key="late-result-command",
        )[0]
        base = utc_now() + timedelta(seconds=1)
        claimed = self.store.claim_operation_jobs(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            lease_seconds=1,
            now=base,
        )[0]
        self.store.start_operation_job(
            self.tenant_a.id,
            job.id,
            self.agent_a.id,
            claimed.lease_token,
            claimed.attempts,
            now=base + timedelta(milliseconds=100),
        )
        self.store.claim_operation_jobs(
            self.tenant_a.id,
            self.agent_a.id,
            self.account_a.id,
            now=base + timedelta(seconds=2),
        )

        completed = self.store.complete_operation_job(
            self.tenant_a.id,
            job.id,
            self.agent_a.id,
            claimed.lease_token,
            claimed.attempts,
            "succeeded",
            result={"evidence": "persisted-before-report"},
            now=base + timedelta(seconds=3),
        )

        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(
            self.store.list_friend_requests(self.tenant_a.id, status="rejected")[0].id,
            request.id,
        )

    def test_postgresql_operation_claim_query_uses_skip_locked(self) -> None:
        statement = TenantStore._operation_claim_candidates_statement(
            self.tenant_a.id,
            self.account_a.id,
            utc_now(),
            10,
            use_skip_locked=True,
        )
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("FOR UPDATE SKIP LOCKED", compiled)

    def test_postgresql_claim_query_uses_skip_locked(self) -> None:
        statement = TenantStore._claim_candidates_statement(
            self.tenant_a.id,
            self.account_a.id,
            utc_now(),
            10,
            use_skip_locked=True,
        )
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)

    def test_database_unique_constraints_are_enforced(self) -> None:
        with self.assertRaises(IntegrityError):
            self.store.create_tenant("tenant-a", "Duplicate")

        user = self.store.create_user("unique@example.com")
        self.store.add_membership(self.tenant_a.id, user.id, "admin")
        with self.assertRaises(IntegrityError):
            self.store.add_membership(self.tenant_a.id, user.id, "viewer")
        with self.assertRaises(IntegrityError):
            self.store.create_channel_account(
                self.tenant_a.id, "wechat", "wx-account"
            )

        message, _ = self._record_message(self.tenant_a.id, self.account_a.id, "unique")
        job = self.store.enqueue_delivery(
            self.tenant_a.id, self.account_a.id, "unique-delivery", {"text": "one"}
        )
        with self.store.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    Message.__table__.insert().values(
                        id=str(uuid.uuid4()),
                        tenant_id=self.tenant_a.id,
                        conversation_id=message.conversation_id,
                        channel_account_id=self.account_a.id,
                        external_message_id=message.external_message_id,
                        direction="inbound",
                        message_type="text",
                        content="duplicate",
                        raw_json={},
                    )
                )
            with self.assertRaises(IntegrityError):
                connection.execute(
                    DeliveryJob.__table__.insert().values(
                        id=str(uuid.uuid4()),
                        tenant_id=self.tenant_a.id,
                        channel_account_id=self.account_a.id,
                        dedupe_key=job.dedupe_key,
                        payload_json={},
                    )
                )

    def test_sqlite_foreign_keys_are_enforced(self) -> None:
        with self.store.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    ChannelAccount.__table__.insert().values(
                        id=str(uuid.uuid4()),
                        tenant_id=str(uuid.uuid4()),
                        channel="wechat",
                        external_account_id="orphan-account",
                    )
                )
            with self.assertRaises(IntegrityError):
                connection.execute(
                    Membership.__table__.insert().values(
                        id=str(uuid.uuid4()),
                        tenant_id=self.tenant_a.id,
                        user_id=str(uuid.uuid4()),
                        role="admin",
                    )
                )

    def test_model_metadata_is_sqlalchemy_declarative(self) -> None:
        self.assertIs(Base.metadata, Message.metadata)
        self.assertIn("delivery_jobs", Base.metadata.tables)


if __name__ == "__main__":
    unittest.main()
