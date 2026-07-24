from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import unittest
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import inspect, text

from wechat_ai.center_runtime import center_diagnostics
from wechat_ai.tenant_store import Base, TenantStore, utc_now


POSTGRES_URL = os.environ.get("CENTER_TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(POSTGRES_URL, "CENTER_TEST_DATABASE_URL is not configured")
class TenantStorePostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = TenantStore(POSTGRES_URL)
        Base.metadata.drop_all(cls.store.engine)
        with cls.store.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        initial_tables = {
            "tenants",
            "users",
            "memberships",
            "channel_accounts",
            "agent_instances",
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
        }
        Base.metadata.create_all(
            cls.store.engine,
            tables=[Base.metadata.tables[name] for name in initial_tables],
        )
        cls.legacy_tenant = cls.store.create_tenant(
            f"legacy-{uuid.uuid4().hex}",
            "Legacy PostgreSQL Tenant",
        )
        cls.store.create_schema()

    @classmethod
    def tearDownClass(cls) -> None:
        Base.metadata.drop_all(cls.store.engine)
        with cls.store.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        cls.store.engine.dispose()

    def test_postgres_schema_and_skip_locked_claim_are_operational(self) -> None:
        diagnostics = center_diagnostics(
            POSTGRES_URL,
            "p" * 32,
            Fernet.generate_key().decode("ascii"),
        )
        self.assertTrue(diagnostics["ok"], diagnostics)
        suffix = uuid.uuid4().hex
        tenant = self.store.create_tenant(f"tenant-{suffix}", "Postgres Tenant")
        account = self.store.create_channel_account(
            tenant.id,
            "wechat_desktop",
            f"wechat-{suffix}",
        )
        first_agent = self.store.register_agent(
            tenant.id,
            account.id,
            f"agent-a-{suffix}",
        )
        second_agent = self.store.register_agent(
            tenant.id,
            account.id,
            f"agent-b-{suffix}",
        )
        operator = self.store.create_user(f"operator-{suffix}@example.com")
        self.store.add_membership(tenant.id, operator.id, "owner")
        job = self.store.enqueue_delivery(
            tenant.id,
            account.id,
            f"delivery-{suffix}",
            {"conversation_external_id": "customer", "content": "reply"},
        )

        def claim(agent_id: str):
            return self.store.claim_deliveries(
                tenant.id,
                agent_id,
                account.id,
                limit=1,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(claim, first_agent.id)
            second_future = executor.submit(claim, second_agent.id)
            claims = first_future.result() + second_future.result()

        self.assertEqual(len(claims), 1)
        claim_result = claims[0]
        self.assertEqual(claim_result.id, job.id)
        self.store.start_delivery(
            tenant.id,
            job.id,
            claim_result.claimed_by_agent_id,
            claim_result.lease_token,
            claim_result.attempts,
        )
        completed = self.store.complete_delivery(
            tenant.id,
            job.id,
            claim_result.claimed_by_agent_id,
            claim_result.lease_token,
            claim_result.attempts,
        )
        self.assertEqual(completed.status, "completed")

        contact = self.store.sync_channel_contacts(
            tenant.id,
            account.id,
            [
                {
                    "external_contact_id": "postgres-customer",
                    "kind": "friend",
                    "display_name": "Postgres Customer",
                }
            ],
            utc_now(),
        )[0]
        self.store.set_chat_policy(
            tenant.id,
            contact.id,
            "ai_auto",
            False,
            operator.id,
        )
        self.store.record_inbound_message(
            tenant.id,
            account.id,
            "postgres-customer",
            f"postgres-message-{suffix}",
            content="question",
            raw_json={},
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_reply = executor.submit(
                self.store.claim_reply_jobs,
                tenant.id,
                limit=1,
            )
            second_reply = executor.submit(
                self.store.claim_reply_jobs,
                tenant.id,
                limit=1,
            )
            reply_claims = first_reply.result() + second_reply.result()
        self.assertEqual(len(reply_claims), 1)
        generated = self.store.complete_reply_job(
            tenant.id,
            reply_claims[0].id,
            reply_claims[0].lease_token,
            "generated reply",
        )
        self.assertEqual(generated.payload_json["content"], "generated reply")

        friend_request = self.store.sync_friend_requests(
            tenant.id,
            account.id,
            [
                {
                    "external_request_id": f"friend-request-{suffix}",
                    "requester_external_id": f"wxid-{suffix}",
                    "display_name": "Concurrent Friend",
                    "status": "pending",
                }
            ],
            utc_now(),
        )[0]
        operation = self.store.enqueue_friend_request_operations(
            tenant.id,
            account.id,
            request_ids=[friend_request.id],
            selection="selected",
            action="accept_friend",
            actor_user_id=operator.id,
            idempotency_key=f"postgres-operation-{suffix}",
        )[0]

        def claim_operation(agent_id: str):
            return self.store.claim_operation_jobs(
                tenant.id,
                agent_id,
                account.id,
                limit=1,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_operation = executor.submit(claim_operation, first_agent.id)
            second_operation = executor.submit(claim_operation, second_agent.id)
            operation_claims = first_operation.result() + second_operation.result()
        self.assertEqual(len(operation_claims), 1)
        claimed_operation = operation_claims[0]
        self.assertEqual(claimed_operation.id, operation.id)
        self.store.start_operation_job(
            tenant.id,
            operation.id,
            claimed_operation.claimed_by_agent_id,
            claimed_operation.lease_token,
            claimed_operation.attempts,
        )
        failed_operation = self.store.complete_operation_job(
            tenant.id,
            operation.id,
            claimed_operation.claimed_by_agent_id,
            claimed_operation.lease_token,
            claimed_operation.attempts,
            "failed",
            error="integration test completion",
        )
        self.assertEqual(failed_operation.status, "failed")

    def test_postgres_legacy_schema_migrates_without_data_loss(self) -> None:
        tenant = self.store.get_tenant(self.legacy_tenant.id)
        self.assertEqual(tenant.name, "Legacy PostgreSQL Tenant")
        self.assertIn("agent_activations", inspect(self.store.engine).get_table_names())


if __name__ == "__main__":
    unittest.main()
