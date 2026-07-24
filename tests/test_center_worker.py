from __future__ import annotations

import unittest
import threading
import time

from cryptography.fernet import Fernet
import httpx

from wechat_ai.center_runtime import bootstrap_tenant
from wechat_ai.center_worker import AICSReplyClient, CenterReplyWorker
from wechat_ai.config import AppConfig
from wechat_ai.tenant_auth import ApiKeyManager, SecretCipher
from wechat_ai.tenant_store import TenantStore, utc_now


def config() -> AppConfig:
    return AppConfig(
        llm_provider="openai_compatible",
        llm_model="base-model",
        ollama_base_url="http://localhost:11434",
        openai_base_url="https://ai.example.test/v1",
        openai_api_key="shared-key",
        request_timeout=30,
        wechat_listen_chats=["*"],
        wechat_self_names=[],
        max_context_messages=8,
        message_buffer_seconds=0,
        dedup_window_seconds=60,
        dry_run=False,
        system_prompt="shared prompt",
        api_retry_attempts=1,
        reply_allowed_chats=["*"],
    )


class FakeLLM:
    def __init__(
        self,
        llm_config: AppConfig,
        calls: list[AppConfig],
        requests: list[dict[str, object]],
    ) -> None:
        self.config = llm_config
        self.calls = calls
        self.requests = requests

    def generate_reply(self, **values: object) -> str:
        self.calls.append(self.config)
        self.requests.append(values)
        return "tenant reply"


class FailingLLM:
    def __init__(self, llm_config: AppConfig) -> None:
        self.config = llm_config

    def generate_reply(self, **values: object) -> str:
        raise RuntimeError(f"provider echoed {self.config.openai_api_key}")


class CenterReplyWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TenantStore("sqlite+pysqlite:///:memory:")
        self.store.create_schema()
        self.bootstrap = bootstrap_tenant(
            self.store,
            ApiKeyManager("p" * 32),
            tenant_slug="worker-tenant",
            tenant_name="Worker Tenant",
            owner_email="worker@example.com",
            channel="wechat_desktop",
            external_account_id="worker-wechat",
            agent_external_id="worker-agent",
        )
        self.cipher = SecretCipher(Fernet.generate_key())

    def _enable_auto_reply(self, external_contact_id: str) -> None:
        contact = self.store.sync_channel_contacts(
            self.bootstrap.tenant_id,
            self.bootstrap.channel_account_id,
            [{"external_contact_id": external_contact_id, "kind": "friend", "display_name": external_contact_id}],
            utc_now(),
        )[0]
        self.store.set_chat_policy(
            self.bootstrap.tenant_id,
            contact.id,
            "ai_auto",
            False,
            self.bootstrap.owner_user_id,
        )

    def test_worker_health_requires_a_running_fresh_loop(self) -> None:
        worker = CenterReplyWorker(self.store, config(), self.cipher)
        stop = threading.Event()
        thread = threading.Thread(
            target=worker.run_forever,
            kwargs={"interval_seconds": 0.01, "stop_event": stop},
        )

        self.assertFalse(worker.health_report()["ok"])
        thread.start()
        deadline = time.monotonic() + 2
        while not worker.health_report()["ok"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(worker.health_report()["ok"])

        stop.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(worker.health_report()["ok"])

    def test_worker_uses_tenant_profile_and_encrypted_credential(self) -> None:
        self._enable_auto_reply("customer-1")
        self.store.upsert_bot_profile(
            self.bootstrap.tenant_id,
            prompt="tenant prompt",
            settings={
                "enabled": True,
                "llm_model": "tenant-model",
                "api_credential_name": "primary",
            },
        )
        self.store.store_api_credential(
            self.bootstrap.tenant_id,
            "llm",
            "primary",
            self.cipher.encrypt("tenant-secret-key"),
        )
        self.store.upsert_knowledge_base(
            self.bootstrap.tenant_id,
            content="Tenant-specific product facts",
        )
        self.store.record_inbound_message(
            self.bootstrap.tenant_id,
            self.bootstrap.channel_account_id,
            "customer-1",
            "message-1",
            content="question",
            raw_json={},
        )
        calls: list[AppConfig] = []
        requests: list[dict[str, object]] = []
        worker = CenterReplyWorker(
            self.store,
            config(),
            self.cipher,
            llm_factory=lambda value: FakeLLM(value, calls, requests),
        )

        stats = worker.process_once()

        self.assertEqual(stats, {"claimed": 1, "generated": 1, "failed": 0})
        self.assertEqual(calls[0].system_prompt, "tenant prompt")
        self.assertEqual(calls[0].llm_model, "tenant-model")
        self.assertEqual(calls[0].openai_api_key, "tenant-secret-key")
        self.assertEqual(
            requests[0]["knowledge_context"],
            "Tenant-specific product facts",
        )
        deliveries = self.store.list_delivery_jobs(self.bootstrap.tenant_id)
        self.assertEqual(deliveries[0].payload_json["content"], "tenant reply")
        self.assertEqual(
            self.store.list_reply_jobs(self.bootstrap.tenant_id)[0].status,
            "generated",
        )

    def test_disabled_profile_leaves_reply_queued_without_calling_ai(self) -> None:
        self._enable_auto_reply("customer-2")
        self.store.upsert_bot_profile(
            self.bootstrap.tenant_id,
            prompt="tenant prompt",
            settings={"enabled": False},
        )
        self.store.record_inbound_message(
            self.bootstrap.tenant_id,
            self.bootstrap.channel_account_id,
            "customer-2",
            "message-2",
            content="question",
            raw_json={},
        )
        worker = CenterReplyWorker(
            self.store,
            config(),
            self.cipher,
            llm_factory=lambda value: (_ for _ in ()).throw(
                AssertionError("LLM must not be created")
            ),
        )

        stats = worker.process_once()

        self.assertEqual(stats, {"claimed": 0, "generated": 0, "failed": 0})
        self.assertEqual(self.store.list_delivery_jobs(self.bootstrap.tenant_id), [])
        job = self.store.list_reply_jobs(self.bootstrap.tenant_id)[0]
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempts, 0)

    def test_worker_redacts_tenant_api_secret_from_failure_state(self) -> None:
        self._enable_auto_reply("customer-3")
        self.store.upsert_bot_profile(
            self.bootstrap.tenant_id,
            prompt="tenant prompt",
            settings={"enabled": True, "api_credential_name": "primary"},
        )
        self.store.store_api_credential(
            self.bootstrap.tenant_id,
            "llm",
            "primary",
            self.cipher.encrypt("tenant-secret-key"),
        )
        self.store.record_inbound_message(
            self.bootstrap.tenant_id,
            self.bootstrap.channel_account_id,
            "customer-3",
            "message-3",
            content="question",
            raw_json={},
        )
        worker = CenterReplyWorker(
            self.store,
            config(),
            self.cipher,
            llm_factory=FailingLLM,
        )

        worker.process_once()

        job = self.store.list_reply_jobs(self.bootstrap.tenant_id)[0]
        self.assertNotIn("tenant-secret-key", job.error)
        self.assertIn("[REDACTED]", job.error)

    def test_ai_cs_provider_receives_identity_only_after_ai_auto_policy(self) -> None:
        self._enable_auto_reply("customer-ai-cs")
        self.store.upsert_bot_profile(
            self.bootstrap.tenant_id,
            prompt="AI-cs owns prompts",
            settings={
                "enabled": True,
                "llm_provider": "ai_cs",
                "openai_base_url": "https://ai-cs.example.test",
                "api_credential_name": "ai-cs",
            },
        )
        self.store.store_api_credential(
            self.bootstrap.tenant_id,
            "llm",
            "ai-cs",
            self.cipher.encrypt("callback-token"),
        )
        self.store.record_inbound_message(
            self.bootstrap.tenant_id,
            self.bootstrap.channel_account_id,
            "conversation-ai-cs",
            "message-ai-cs",
            content="customer question",
            raw_json={},
            sender_external_id="customer-ai-cs",
            customer_display_name="Customer A",
        )
        calls: list[AppConfig] = []
        requests: list[dict[str, object]] = []
        worker = CenterReplyWorker(
            self.store,
            config(),
            self.cipher,
            llm_factory=lambda value: (_ for _ in ()).throw(
                AssertionError("the local LLM must not be used for ai_cs")
            ),
            ai_cs_factory=lambda value: FakeLLM(value, calls, requests),
        )

        stats = worker.process_once()

        self.assertEqual(stats, {"claimed": 1, "generated": 1, "failed": 0})
        self.assertEqual(calls[0].llm_provider, "ai_cs")
        self.assertEqual(calls[0].openai_api_key, "callback-token")
        self.assertEqual(
            requests[0],
            {
                "channel_account_id": self.bootstrap.channel_account_id,
                "conversation_id": "conversation-ai-cs",
                "contact_external_id": "customer-ai-cs",
                "contact_display_name": "Customer A",
                "source_message_id": "message-ai-cs",
                "message": "customer question",
                "message_type": "text",
            },
        )
        self.assertEqual(
            self.store.list_delivery_jobs(self.bootstrap.tenant_id)[0].payload_json["content"],
            "tenant reply",
        )

    def test_ai_cs_http_client_sends_identity_and_combines_messages(self) -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"messages": [{"text": "first"}, {"text": "second"}]},
            )

        client = AICSReplyClient(
            config().with_overrides(
                llm_provider="ai_cs",
                openai_base_url="https://ai-cs.example.test",
                openai_api_key="callback-token",
            )
        )
        client._client.close()
        client._client = httpx.Client(
            base_url="https://ai-cs.example.test/",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer callback-token"},
        )
        try:
            reply = client.generate_reply(
                channel_account_id="channel-a",
                conversation_id="conversation-a",
                contact_external_id="contact-a",
                contact_display_name="Customer A",
                source_message_id="message-a",
                message="question",
                message_type="text",
            )
        finally:
            client._client.close()

        self.assertEqual(reply, "first\nsecond")
        self.assertEqual(requests[0].url.path, "/v1/personal-wechat/reply")
        self.assertEqual(requests[0].headers["authorization"], "Bearer callback-token")
        self.assertIn(b'"contact_external_id":"contact-a"', requests[0].content)


if __name__ == "__main__":
    unittest.main()
