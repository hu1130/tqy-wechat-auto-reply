from __future__ import annotations

import json
import unittest
from http.server import ThreadingHTTPServer
import threading
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import text

from wechat_ai.center_agent import CenterAgentClient, DeliveryResult, DeliveryStart, InboundEnvelope
from wechat_ai.center_runtime import (
    AgentActivationSecurity,
    CenterAuthenticator,
    CenterProvisioningSecurity,
    bootstrap_tenant,
    build_center_application,
)
from wechat_ai.center_server import CenterApplication, build_center_handler
from wechat_ai.config import load_config, validate_config
from wechat_ai.reliability import RetryPolicy
from wechat_ai.tenant_auth import ApiKeyManager, SecretCipher
from wechat_ai.tenant_store import TenantStore, utc_now


class AgentActivationSecurityTests(unittest.TestCase):
    def test_wechat_identity_hash_is_deterministic_and_domain_separated(self) -> None:
        security = AgentActivationSecurity("p" * 32)
        canonical = '{"account_id":"wxid_account_a"}'

        first = security.hash_wechat_identity(canonical)
        repeated = security.hash_wechat_identity(canonical)
        changed = security.hash_wechat_identity('{"account_id":"wxid_account_b"}')

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, changed)
        self.assertNotEqual(first, security.hash_device_id(canonical))
        self.assertEqual(len(first), 64)
        with self.assertRaisesRegex(ValueError, "identity is required"):
            security.hash_wechat_identity("")
        with self.assertRaisesRegex(ValueError, "identity is required"):
            security.hash_wechat_identity("   ")

    def test_digest_inputs_are_bounded_and_fail_closed(self) -> None:
        security = AgentActivationSecurity("p" * 32)

        with self.assertRaisesRegex(ValueError, "activation_failed"):
            security.hash_activation_code("wxact_" + "x" * 123)
        with self.assertRaisesRegex(ValueError, "activation_failed"):
            security.hash_device_id("x" * 256)
        with self.assertRaisesRegex(ValueError, "identity is required"):
            security.hash_wechat_identity("x" * 2049)


class PepperConfigurationTests(unittest.TestCase):
    def test_loads_previous_peppers_without_exposing_their_values(self) -> None:
        previous = ["a" * 32, "b" * 32]
        with patch.dict(
            "os.environ",
            {"CENTER_API_KEY_PREVIOUS_PEPPERS": ",".join(previous)},
            clear=True,
        ):
            config = load_config("__missing_center_test_env__")

        self.assertEqual(config.center_api_key_previous_peppers, previous)
        public = config.to_public_dict()
        self.assertTrue(public["center_api_key_previous_peppers_set"])
        self.assertNotIn(previous[0], repr(public))
        self.assertNotIn(previous[1], repr(public))

    def test_rejects_empty_previous_pepper_entries(self) -> None:
        with patch.dict(
            "os.environ",
            {"CENTER_API_KEY_PREVIOUS_PEPPERS": f"{'a' * 32},,"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "empty entries"):
                load_config("__missing_center_test_env__")

    def test_production_validation_reports_invalid_key_ring_without_secrets(self) -> None:
        secret = "secret-value-" + "x" * 32
        with patch.dict("os.environ", {}, clear=True):
            config = load_config("__missing_center_test_env__")
        config = config.with_overrides(
            center_database_url="postgresql+psycopg://db.example/center",
            center_api_key_pepper=secret,
            center_api_key_previous_peppers=[secret],
            center_secret_key=Fernet.generate_key().decode("ascii"),
            center_provisioning_token="t" * 32,
        )

        warnings = validate_config(config)

        self.assertTrue(
            any("key ring is invalid" in warning for warning in warnings)
        )
        self.assertNotIn(secret, repr(warnings))


class CenterProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TenantStore("sqlite+pysqlite:///:memory:")
        self.store.create_schema()
        self.keys = ApiKeyManager("p" * 32)
        self.provisioning = CenterProvisioningSecurity(
            "s" * 32,
            self.keys,
            store=self.store,
        )
        auth = CenterAuthenticator(self.store, self.keys)
        self.app = CenterApplication(
            self.store,
            auth.authenticate_tenant,
            auth.authenticate_agent,
            SecretCipher(Fernet.generate_key()),
            activation_security=AgentActivationSecurity("p" * 32, self.keys),
            provisioning_security=self.provisioning,
        )

    def _provision(self, external_id: str = "ai-cs-tenant-1"):
        return self.app.dispatch(
            "POST",
            "/v1/provisioning/personal-wechat",
            {
                "Authorization": f"Bearer {'s' * 32}",
                "Idempotency-Key": f"personal-wechat:{external_id}",
            },
            {
                "external_tenant_id": external_id,
                "tenant_name": "Customer A",
                "owner_email": "owner@example.com",
            },
        )

    def test_provisioning_is_idempotent_and_returned_key_authenticates(self) -> None:
        first = self._provision()
        repeated = self._provision()

        self.assertEqual(first.status, 201)
        self.assertEqual(repeated.status, 200)
        self.assertTrue(first.body["created"])
        self.assertFalse(repeated.body["created"])
        for field in (
            "center_tenant_id",
            "center_tenant_api_key",
            "channel_account_id",
            "agent_id",
        ):
            self.assertEqual(first.body[field], repeated.body[field])

        tenant = self.app.dispatch(
            "GET",
            "/v1/tenant",
            {
                "Authorization": f"Bearer {first.body['center_tenant_api_key']}",
                "X-Tenant-ID": str(first.body["center_tenant_id"]),
            },
        )
        self.assertEqual(tenant.status, 200)
        with self.store.engine.connect() as connection:
            stored_hashes = connection.execute(
                text("SELECT key_hash FROM access_credentials")
            ).scalars().all()
        self.assertNotIn(first.body["center_tenant_api_key"], stored_hashes)

    def test_rotating_provisioning_token_does_not_change_tenant_material(self) -> None:
        original = self.provisioning.derive("ai-cs-tenant-1")
        rotated = CenterProvisioningSecurity("t" * 32, self.keys).derive(
            "ai-cs-tenant-1"
        )

        self.assertEqual(original.tenant_slug, rotated.tenant_slug)
        self.assertEqual(original.channel_external_account_id, rotated.channel_external_account_id)
        self.assertEqual(original.agent_external_id, rotated.agent_external_id)
        self.assertEqual(original.tenant_api_key, rotated.tenant_api_key)
        self.assertFalse(self.provisioning.authenticate("t" * 32))

    def test_pepper_rotation_preserves_existing_provisioning_material(self) -> None:
        first = self._provision()
        rotated_keys = ApiKeyManager(
            "n" * 32,
            previous_peppers=["p" * 32],
        )
        rotated_provisioning = CenterProvisioningSecurity(
            "s" * 32,
            rotated_keys,
            store=self.store,
        )
        rotated_auth = CenterAuthenticator(self.store, rotated_keys)
        rotated_app = CenterApplication(
            self.store,
            rotated_auth.authenticate_tenant,
            rotated_auth.authenticate_agent,
            SecretCipher(Fernet.generate_key()),
            activation_security=AgentActivationSecurity(
                "n" * 32,
                rotated_keys,
                store=self.store,
            ),
            provisioning_security=rotated_provisioning,
        )

        repeated = rotated_app.dispatch(
            "POST",
            "/v1/provisioning/personal-wechat",
            {
                "Authorization": f"Bearer {'s' * 32}",
                "Idempotency-Key": "personal-wechat:ai-cs-tenant-1",
            },
            {
                "external_tenant_id": "ai-cs-tenant-1",
                "tenant_name": "Customer A",
                "owner_email": "owner@example.com",
            },
        )
        new_tenant = rotated_provisioning.derive("ai-cs-tenant-2")

        self.assertEqual(repeated.status, 200)
        self.assertEqual(
            repeated.body["center_tenant_api_key"],
            first.body["center_tenant_api_key"],
        )
        self.assertFalse(
            self.keys.verify(
                new_tenant.tenant_api_key,
                new_tenant.tenant_key_record,
            )
        )

    def test_provisioning_fails_closed_for_auth_idempotency_and_shape(self) -> None:
        payload = {
            "external_tenant_id": "ai-cs-tenant-1",
            "tenant_name": "Customer A",
            "owner_email": "owner@example.com",
        }
        wrong_token = self.app.dispatch(
            "POST",
            "/v1/provisioning/personal-wechat",
            {
                "Authorization": f"Bearer {'x' * 32}",
                "Idempotency-Key": "personal-wechat:ai-cs-tenant-1",
            },
            payload,
        )
        wrong_key = self.app.dispatch(
            "POST",
            "/v1/provisioning/personal-wechat",
            {
                "Authorization": f"Bearer {'s' * 32}",
                "Idempotency-Key": "wrong",
            },
            payload,
        )
        unknown_field = self.app.dispatch(
            "POST",
            "/v1/provisioning/personal-wechat",
            {
                "Authorization": f"Bearer {'s' * 32}",
                "Idempotency-Key": "personal-wechat:ai-cs-tenant-1",
            },
            {**payload, "agent_token": "must-not-be-accepted"},
        )

        self.assertEqual(wrong_token.status, 401)
        self.assertEqual(wrong_key.status, 400)
        self.assertEqual(unknown_field.status, 400)


class CenterRuntimeEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TenantStore("sqlite+pysqlite:///:memory:")
        self.store.create_schema()
        self.keys = ApiKeyManager("p" * 32)
        self.tenant_a = bootstrap_tenant(
            self.store,
            self.keys,
            tenant_slug="tenant-a",
            tenant_name="Tenant A",
            owner_email="owner-a@example.com",
            channel="wechat_desktop",
            external_account_id="wechat-a",
            agent_external_id="agent-a",
        )
        self.tenant_b = bootstrap_tenant(
            self.store,
            self.keys,
            tenant_slug="tenant-b",
            tenant_name="Tenant B",
            owner_email="owner-b@example.com",
            channel="wechat_desktop",
            external_account_id="wechat-b",
            agent_external_id="agent-b",
        )
        auth = CenterAuthenticator(self.store, self.keys)
        self.cipher = SecretCipher(Fernet.generate_key())
        self.activation_security = AgentActivationSecurity("p" * 32, self.keys)
        self.app = CenterApplication(
            self.store,
            auth.authenticate_tenant,
            auth.authenticate_agent,
            self.cipher,
            activation_security=self.activation_security,
        )
        for bundle, account_id in (
            (self.tenant_a, "wxid_tenant_a"),
            (self.tenant_b, "wxid_tenant_b"),
        ):
            identity = {"account_id": account_id, "nickname": bundle.tenant_id}
            canonical = json.dumps(
                {"account_id": account_id},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            self.store.observe_channel_wechat_identity(
                bundle.tenant_id,
                bundle.channel_account_id,
                identity,
                self.activation_security.hash_wechat_identity(canonical),
            )
            self.store.confirm_channel_wechat_identity(
                bundle.tenant_id,
                bundle.channel_account_id,
                bundle.owner_user_id,
                f"test-confirm-{bundle.tenant_id}",
            )

    def test_two_tenants_are_isolated_through_auth_and_delivery_lifecycle(self) -> None:
        contact = self.store.sync_channel_contacts(
            self.tenant_a.tenant_id,
            self.tenant_a.channel_account_id,
            [{"external_contact_id": "customer-a", "kind": "friend", "display_name": "customer-a"}],
            utc_now(),
        )[0]
        self.store.set_chat_policy(
            self.tenant_a.tenant_id,
            contact.id,
            "ai_auto",
            False,
            self.tenant_a.owner_user_id,
        )
        inbound = {
            "tenant_id": self.tenant_a.tenant_id,
            "agent_id": self.tenant_a.agent_id,
            "channel_account_id": self.tenant_a.channel_account_id,
            "external_message_id": "wx-a-1",
            "conversation_external_id": "customer-a",
            "sender_external_id": "customer-a",
            "message_type": "text",
            "content": "hello A",
            "occurred_at": "2026-07-21T08:00:00Z",
        }
        accepted = self.app.dispatch(
            "POST",
            "/v1/agent/inbound",
            {
                "Authorization": f"Bearer {self.tenant_a.agent_token}",
                "Idempotency-Key": "wx-a-1",
            },
            inbound,
        )
        duplicate = self.app.dispatch(
            "POST",
            "/v1/agent/inbound",
            {
                "Authorization": f"Bearer {self.tenant_a.agent_token}",
                "Idempotency-Key": "wx-a-1",
            },
            inbound,
        )
        self.assertEqual(accepted.status, 200)
        self.assertEqual(duplicate.status, 200)
        self.assertEqual(len(self.store.list_messages(self.tenant_a.tenant_id)), 1)
        self.assertEqual(self.store.list_messages(self.tenant_b.tenant_id), [])

        wrong_tenant = self.app.dispatch(
            "GET",
            "/v1/messages",
            {
                "Authorization": f"Bearer {self.tenant_a.tenant_api_key}",
                "X-Tenant-ID": self.tenant_b.tenant_id,
            },
        )
        self.assertEqual(wrong_tenant.status, 401)

        message = self.store.list_messages(self.tenant_a.tenant_id)[0]
        queued = self.app.dispatch(
            "POST",
            "/v1/deliveries",
            {
                "Authorization": f"Bearer {self.tenant_a.tenant_api_key}",
                "X-Tenant-ID": self.tenant_a.tenant_id,
                "Idempotency-Key": "reply-a-1",
            },
            {
                "channel_account_id": self.tenant_a.channel_account_id,
                "conversation_id": message.conversation_id,
                "content": "reply A",
            },
        )
        self.assertEqual(queued.status, 201)

        claim = self.app.dispatch(
            "POST",
            "/v1/agent/deliveries/claim",
            {"Authorization": f"Bearer {self.tenant_a.agent_token}"},
            {
                "tenant_id": self.tenant_a.tenant_id,
                "agent_id": self.tenant_a.agent_id,
                "channel_account_id": self.tenant_a.channel_account_id,
                "limit": 10,
            },
        )
        self.assertEqual(claim.status, 200)
        command = claim.body["deliveries"][0]
        self.assertEqual(command["content"], "reply A")

        start = {
            "tenant_id": self.tenant_a.tenant_id,
            "agent_id": self.tenant_a.agent_id,
            "channel_account_id": self.tenant_a.channel_account_id,
            "delivery_id": command["delivery_id"],
            "lease_token": command["lease_token"],
            "attempt": command["attempt"],
        }
        started = self.app.dispatch(
            "POST",
            "/v1/agent/deliveries/start",
            {
                "Authorization": f"Bearer {self.tenant_a.agent_token}",
                "Idempotency-Key": f"{command['delivery_id']}:{command['attempt']}:start",
            },
            start,
        )
        completed = self.app.dispatch(
            "POST",
            "/v1/agent/deliveries/complete",
            {
                "Authorization": f"Bearer {self.tenant_a.agent_token}",
                "Idempotency-Key": f"{command['delivery_id']}:{command['attempt']}",
            },
            {
                **start,
                "status": "sent",
                "completed_at": "2026-07-21T08:01:00Z",
                "error": None,
            },
        )
        self.assertEqual(started.status, 200)
        self.assertEqual(completed.status, 200)
        self.assertEqual(
            self.store.list_delivery_jobs(self.tenant_a.tenant_id)[0].status,
            "completed",
        )
        self.assertEqual(
            [item.direction for item in self.store.list_messages(self.tenant_a.tenant_id)],
            ["inbound", "outbound"],
        )
        reply_jobs = self.app.dispatch(
            "GET",
            "/v1/reply-jobs",
            {
                "Authorization": f"Bearer {self.tenant_a.tenant_api_key}",
                "X-Tenant-ID": self.tenant_a.tenant_id,
            },
        )
        self.assertEqual(reply_jobs.status, 200)
        self.assertNotIn("lease_token", reply_jobs.body["reply_jobs"][0])

    def test_agent_token_cannot_cross_tenant_or_channel_binding(self) -> None:
        response = self.app.dispatch(
            "POST",
            "/v1/agent/deliveries/claim",
            {"Authorization": f"Bearer {self.tenant_a.agent_token}"},
            {
                "tenant_id": self.tenant_b.tenant_id,
                "agent_id": self.tenant_b.agent_id,
                "channel_account_id": self.tenant_b.channel_account_id,
                "limit": 1,
            },
        )
        self.assertEqual(response.status, 401)

    def test_agent_activation_rotates_token_once_and_never_persists_plaintext(self) -> None:
        heartbeat = {
            "tenant_id": self.tenant_a.tenant_id,
            "agent_id": self.tenant_a.agent_id,
            "channel_account_id": self.tenant_a.channel_account_id,
            "status": "online",
            "observed_at": "2026-07-22T08:00:00Z",
            "wechat_identity": {
                "account_id": "wxid_tenant_a",
                "nickname": self.tenant_a.tenant_id,
            },
        }
        old_token_before = self.app.dispatch(
            "POST",
            "/v1/agent/heartbeat",
            {"Authorization": f"Bearer {self.tenant_a.agent_token}"},
            heartbeat,
        )
        invalid_activation = self.app.dispatch(
            "POST",
            "/v1/agent/activate",
            {},
            {
                "activation_code": "wxact_invalid-code",
                "device_id": "device-a",
                "device_name": "Reception PC",
                "agent_version": "1.0.0",
            },
        )
        old_token_after_failed_activation = self.app.dispatch(
            "POST",
            "/v1/agent/heartbeat",
            {"Authorization": f"Bearer {self.tenant_a.agent_token}"},
            heartbeat,
        )
        management_headers = {
            "Authorization": f"Bearer {self.tenant_a.tenant_api_key}",
            "X-Tenant-ID": self.tenant_a.tenant_id,
            "Idempotency-Key": "install-reception-agent",
        }
        activation_payload = {
            "agent_id": self.tenant_a.agent_id,
            "channel_account_id": self.tenant_a.channel_account_id,
            "expires_in_seconds": 900,
        }
        created = self.app.dispatch(
            "POST",
            "/v1/agent-activations",
            management_headers,
            activation_payload,
        )
        repeated_creation = self.app.dispatch(
            "POST",
            "/v1/agent-activations",
            management_headers,
            activation_payload,
        )
        activation_code = created.body["activation_code"]
        activated = self.app.dispatch(
            "POST",
            "/v1/agent/activate",
            {},
            {
                "activation_code": activation_code,
                "device_id": "device-a",
                "device_name": "Reception PC",
                "agent_version": "1.0.0",
            },
        )
        new_token = activated.body["agent_token"]
        old_token_after_success = self.app.dispatch(
            "POST",
            "/v1/agent/heartbeat",
            {"Authorization": f"Bearer {self.tenant_a.agent_token}"},
            heartbeat,
        )
        new_token_after_success = self.app.dispatch(
            "POST",
            "/v1/agent/heartbeat",
            {"Authorization": f"Bearer {new_token}"},
            heartbeat,
        )
        replay = self.app.dispatch(
            "POST",
            "/v1/agent/activate",
            {},
            {
                "activation_code": activation_code,
                "device_id": "device-b",
                "device_name": "Other PC",
                "agent_version": "1.0.1",
            },
        )
        new_token_after_replay = self.app.dispatch(
            "POST",
            "/v1/agent/heartbeat",
            {"Authorization": f"Bearer {new_token}"},
            heartbeat,
        )

        self.assertEqual(old_token_before.status, 200)
        self.assertEqual(invalid_activation.status, 400)
        self.assertEqual(old_token_after_failed_activation.status, 200)
        self.assertEqual((created.status, repeated_creation.status), (201, 200))
        self.assertEqual(created.body, repeated_creation.body)
        self.assertTrue(activation_code.startswith("wxact_"))
        self.assertEqual(activated.status, 200)
        self.assertEqual(old_token_after_success.status, 401)
        self.assertEqual(new_token_after_success.status, 200)
        self.assertEqual(replay.status, 400)
        self.assertEqual(replay.body["error"], "activation_failed")
        self.assertEqual(new_token_after_replay.status, 200)
        self.assertNotIn("code_hash", created.body["activation"])

        with self.store.engine.connect() as connection:
            activation_rows = connection.execute(
                text("SELECT * FROM agent_activations")
            ).mappings().all()
            credential_rows = connection.execute(
                text("SELECT * FROM access_credentials")
            ).mappings().all()
        stored_dump = repr((activation_rows, credential_rows))
        self.assertNotIn(activation_code, stored_dump)
        self.assertNotIn(new_token, stored_dump)

    def test_api_key_pepper_rotation_keeps_old_credentials_and_activation_valid(self) -> None:
        management_headers = {
            "Authorization": f"Bearer {self.tenant_a.tenant_api_key}",
            "X-Tenant-ID": self.tenant_a.tenant_id,
            "Idempotency-Key": "pepper-rotation-activation",
        }
        activation_payload = {
            "agent_id": self.tenant_a.agent_id,
            "channel_account_id": self.tenant_a.channel_account_id,
            "expires_in_seconds": 900,
        }
        created_before_rotation = self.app.dispatch(
            "POST",
            "/v1/agent-activations",
            management_headers,
            activation_payload,
        )
        old_activation_code = created_before_rotation.body["activation_code"]

        rotated_keys = ApiKeyManager(
            "n" * 32,
            previous_peppers=["p" * 32],
        )
        rotated_auth = CenterAuthenticator(self.store, rotated_keys)
        rotated_security = AgentActivationSecurity(
            "n" * 32,
            rotated_keys,
            store=self.store,
        )
        rotated_app = CenterApplication(
            self.store,
            rotated_auth.authenticate_tenant,
            rotated_auth.authenticate_agent,
            self.cipher,
            activation_security=rotated_security,
        )
        heartbeat = {
            "tenant_id": self.tenant_a.tenant_id,
            "agent_id": self.tenant_a.agent_id,
            "channel_account_id": self.tenant_a.channel_account_id,
            "status": "online",
            "observed_at": "2026-07-22T08:00:00Z",
            "wechat_identity": {
                "account_id": "wxid_tenant_a",
                "nickname": self.tenant_a.tenant_id,
            },
        }

        old_agent_token_during_rotation = rotated_app.dispatch(
            "POST",
            "/v1/agent/heartbeat",
            {"Authorization": f"Bearer {self.tenant_a.agent_token}"},
            heartbeat,
        )
        repeated_after_rotation = rotated_app.dispatch(
            "POST",
            "/v1/agent-activations",
            management_headers,
            activation_payload,
        )
        activated = rotated_app.dispatch(
            "POST",
            "/v1/agent/activate",
            {},
            {
                "activation_code": old_activation_code,
                "device_id": "device-after-rotation",
                "device_name": "Reception PC",
                "agent_version": "1.1.0",
            },
        )
        new_agent_token = activated.body["agent_token"]

        current_only_keys = ApiKeyManager("n" * 32)
        current_only_auth = CenterAuthenticator(self.store, current_only_keys)
        current_only_app = CenterApplication(
            self.store,
            current_only_auth.authenticate_tenant,
            current_only_auth.authenticate_agent,
            self.cipher,
            activation_security=AgentActivationSecurity(
                "n" * 32,
                current_only_keys,
                store=self.store,
            ),
        )
        old_tenant_key_after_removal = current_only_app.dispatch(
            "GET",
            "/v1/tenant",
            {
                "Authorization": f"Bearer {self.tenant_a.tenant_api_key}",
                "X-Tenant-ID": self.tenant_a.tenant_id,
            },
        )
        new_agent_token_after_removal = current_only_app.dispatch(
            "POST",
            "/v1/agent/heartbeat",
            {"Authorization": f"Bearer {new_agent_token}"},
            heartbeat,
        )

        self.assertEqual(created_before_rotation.status, 201)
        self.assertEqual(old_agent_token_during_rotation.status, 200)
        self.assertEqual(
            old_agent_token_during_rotation.body["identity_status"],
            "verified",
        )
        self.assertEqual(repeated_after_rotation.status, 200)
        self.assertEqual(
            repeated_after_rotation.body["activation_code"],
            old_activation_code,
        )
        self.assertEqual(activated.status, 200)
        self.assertEqual(old_tenant_key_after_removal.status, 401)
        self.assertEqual(new_agent_token_after_removal.status, 200)
        self.assertEqual(
            new_agent_token_after_removal.body["identity_status"],
            "verified",
        )
        self.assertFalse(
            self.keys.verify(
                new_agent_token,
                rotated_keys.record_for_secret(
                    new_agent_token,
                    expected_prefix=ApiKeyManager.AGENT_TOKEN_PREFIX,
                ),
            )
        )

        with self.store.engine.connect() as connection:
            consumed_device_hash = connection.execute(
                text(
                    "SELECT consumed_device_hash FROM agent_activations "
                    "WHERE idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": "pepper-rotation-activation"},
            ).scalar_one()
        self.assertEqual(
            consumed_device_hash,
            rotated_security.hash_device_id("device-after-rotation"),
        )
        self.assertNotEqual(
            consumed_device_hash,
            self.activation_security.hash_device_id("device-after-rotation"),
        )

    def test_invalid_pepper_ring_is_rejected_before_database_initialization(self) -> None:
        secret_key = Fernet.generate_key().decode("ascii")

        with self.assertRaises(ValueError):
            build_center_application(
                "not-a-database-url",
                "n" * 32,
                secret_key,
                previous_api_key_peppers=["short"],
            )
        with self.assertRaises(ValueError):
            build_center_application(
                "not-a-database-url",
                "n" * 32,
                secret_key,
                previous_api_key_peppers=["n" * 32],
            )
        with patch.dict(
            "os.environ",
            {"CENTER_API_KEY_PREVIOUS_PEPPERS": "short"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "previous pepper"):
                build_center_application(
                    "not-a-database-url",
                    "n" * 32,
                    secret_key,
                )

    def test_real_http_handler_matches_agent_client_protocol(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), build_center_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = CenterAgentClient(
            f"http://127.0.0.1:{server.server_port}",
            self.tenant_a.agent_token,
            self.tenant_a.tenant_id,
            self.tenant_a.agent_id,
            self.tenant_a.channel_account_id,
            allow_insecure_http=True,
            retry_policy=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0),
        )
        try:
            client.report_inbound(
                InboundEnvelope(
                    self.tenant_a.tenant_id,
                    self.tenant_a.agent_id,
                    self.tenant_a.channel_account_id,
                    "http-message-1",
                    "http-customer",
                    "http-customer",
                    "text",
                    "hello over HTTP",
                    "2026-07-21T08:00:00Z",
                )
            )
            message = self.store.list_messages(self.tenant_a.tenant_id)[0]
            self.store.enqueue_delivery(
                self.tenant_a.tenant_id,
                self.tenant_a.channel_account_id,
                "http-reply-1",
                {
                    "conversation_external_id": "http-customer",
                    "content": "HTTP reply",
                },
                conversation_id=message.conversation_id,
            )
            command = client.claim_deliveries(1)[0]
            client.start_delivery(
                DeliveryStart(
                    command.tenant_id,
                    command.agent_id,
                    command.channel_account_id,
                    command.delivery_id,
                    command.lease_token,
                    command.attempt,
                )
            )
            client.complete_delivery(
                DeliveryResult(
                    command.tenant_id,
                    command.agent_id,
                    command.channel_account_id,
                    command.delivery_id,
                    command.lease_token,
                    command.attempt,
                    "sent",
                    "2026-07-21T08:01:00Z",
                )
            )
            self.assertEqual(
                self.store.list_delivery_jobs(self.tenant_a.tenant_id)[0].status,
                "completed",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_owner_can_idempotently_resolve_uncertain_delivery_with_audit(self) -> None:
        message, _ = self.store.record_inbound_message(
            self.tenant_a.tenant_id,
            self.tenant_a.channel_account_id,
            "resolve-customer",
            "resolve-message",
            content="question",
            raw_json={},
        )
        job = self.store.enqueue_delivery(
            self.tenant_a.tenant_id,
            self.tenant_a.channel_account_id,
            "resolve-delivery",
            {"conversation_external_id": "resolve-customer", "content": "reply"},
            conversation_id=message.conversation_id,
        )
        claim = self.store.claim_deliveries(
            self.tenant_a.tenant_id,
            self.tenant_a.agent_id,
            self.tenant_a.channel_account_id,
        )[0]
        self.store.start_delivery(
            self.tenant_a.tenant_id,
            job.id,
            self.tenant_a.agent_id,
            claim.lease_token,
            claim.attempts,
        )
        self.store.complete_delivery(
            self.tenant_a.tenant_id,
            job.id,
            self.tenant_a.agent_id,
            claim.lease_token,
            claim.attempts,
            result_status="uncertain",
            error="Agent restarted during send",
        )
        headers = {
            "Authorization": f"Bearer {self.tenant_a.tenant_api_key}",
            "X-Tenant-ID": self.tenant_a.tenant_id,
            "Idempotency-Key": "resolve-once",
        }
        payload = {
            "resolution": "sent",
            "note": "Operator verified the message in WeChat",
        }

        first = self.app.dispatch(
            "POST",
            f"/v1/deliveries/{job.id}/resolve",
            headers,
            payload,
        )
        repeated = self.app.dispatch(
            "POST",
            f"/v1/deliveries/{job.id}/resolve",
            headers,
            payload,
        )
        audit = self.app.dispatch(
            "GET",
            "/v1/audit",
            {
                "Authorization": f"Bearer {self.tenant_a.tenant_api_key}",
                "X-Tenant-ID": self.tenant_a.tenant_id,
            },
        )

        self.assertEqual(first.status, 200)
        self.assertEqual(repeated.status, 200)
        self.assertEqual(first.body["delivery"]["status"], "completed")
        self.assertEqual(audit.status, 200)
        self.assertEqual(audit.body["events"][0]["action"], "delivery.uncertain_resolved")

    def test_owner_configures_tenant_bot_and_encrypted_ai_credential(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.tenant_a.tenant_api_key}",
            "X-Tenant-ID": self.tenant_a.tenant_id,
        }
        credential = self.app.dispatch(
            "PUT",
            "/v1/ai-credentials/primary",
            headers,
            {"value": "tenant-ai-secret"},
        )
        profile = self.app.dispatch(
            "PUT",
            "/v1/bot-profile",
            headers,
            {
                "name": "default",
                "prompt": "Reply as Tenant A support.",
                "settings": {
                    "enabled": True,
                    "llm_model": "tenant-model",
                    "api_credential_name": "primary",
                },
            },
        )
        knowledge = self.app.dispatch(
            "PUT",
            "/v1/knowledge-base",
            headers,
            {
                "name": "default",
                "status": "active",
                "content": "Tenant A product facts",
            },
        )
        loaded = self.app.dispatch("GET", "/v1/bot-profile", headers)
        loaded_knowledge = self.app.dispatch("GET", "/v1/knowledge-base", headers)

        self.assertEqual(credential.status, 200)
        self.assertNotIn("tenant-ai-secret", str(credential.body))
        stored = self.store.get_api_credential(self.tenant_a.tenant_id, "llm", "primary")
        self.assertNotEqual(stored.encrypted_value, "tenant-ai-secret")
        self.assertEqual(self.cipher.decrypt(stored.encrypted_value), "tenant-ai-secret")
        self.assertEqual(profile.status, 200)
        self.assertEqual(loaded.body["profile"]["settings_json"]["enabled"], True)
        self.assertEqual(knowledge.status, 200)
        self.assertEqual(
            loaded_knowledge.body["knowledge_base"]["content"],
            "Tenant A product facts",
        )

    def test_friend_request_operation_lifecycle_creates_policy_and_welcome_dependency(self) -> None:
        agent_headers = {
            "Authorization": f"Bearer {self.tenant_a.agent_token}",
            "Idempotency-Key": "friend-snapshot-1",
        }
        synced = self.app.dispatch(
            "POST",
            "/v1/agent/friend-requests/sync",
            agent_headers,
            {
                "tenant_id": self.tenant_a.tenant_id,
                "agent_id": self.tenant_a.agent_id,
                "channel_account_id": self.tenant_a.channel_account_id,
                "snapshot_id": "friend-snapshot-1",
                "observed_at": "2026-07-22T08:00:00Z",
                "requests": [
                    {
                        "external_request_id": "wxfr_alice",
                        "display_name": "Alice",
                        "verification_message": "Product inquiry",
                        "requested_at": "2026-07-22T07:59:00Z",
                        "status": "pending",
                        "raw": {"source": "wechat_ui"},
                    }
                ],
            },
        )
        tenant_headers = {
            "Authorization": f"Bearer {self.tenant_a.tenant_api_key}",
            "X-Tenant-ID": self.tenant_a.tenant_id,
        }
        pending = self.app.dispatch(
            "GET",
            f"/v1/friend-requests?channel_account_id={self.tenant_a.channel_account_id}&status=pending",
            tenant_headers,
        )
        request_id = pending.body["friend_requests"][0]["id"]
        limits = self.app.dispatch(
            "PUT",
            f"/v1/operation-limits/{self.tenant_a.channel_account_id}",
            {**tenant_headers, "Idempotency-Key": "limits-1"},
            {
                "batch_size": 10,
                "min_interval_seconds": 5,
                "max_per_hour": 30,
                "max_per_day": 100,
                "paused": False,
                "welcome_message": "Welcome, Alice",
                "auto_enable_reply": True,
            },
        )
        queued = self.app.dispatch(
            "POST",
            "/v1/friend-requests/approve-selected",
            {**tenant_headers, "Idempotency-Key": "approve-alice-1"},
            {
                "channel_account_id": self.tenant_a.channel_account_id,
                "friend_request_ids": [request_id],
                "send_welcome_message": True,
                "welcome_message": "Welcome, Alice",
                "auto_enable_reply": True,
            },
        )
        claim = self.app.dispatch(
            "POST",
            "/v1/agent/operations/claim",
            {"Authorization": f"Bearer {self.tenant_a.agent_token}"},
            {
                "tenant_id": self.tenant_a.tenant_id,
                "agent_id": self.tenant_a.agent_id,
                "channel_account_id": self.tenant_a.channel_account_id,
                "limit": 1,
            },
        )
        command = claim.body["operations"][0]
        identity = {
            "tenant_id": self.tenant_a.tenant_id,
            "agent_id": self.tenant_a.agent_id,
            "channel_account_id": self.tenant_a.channel_account_id,
            "operation_id": command["operation_id"],
            "lease_token": command["lease_token"],
            "attempt": command["attempt"],
        }
        started = self.app.dispatch(
            "POST",
            "/v1/agent/operations/start",
            {
                "Authorization": f"Bearer {self.tenant_a.agent_token}",
                "Idempotency-Key": f"{command['operation_id']}:{command['attempt']}:start",
            },
            identity,
        )
        completed = self.app.dispatch(
            "POST",
            "/v1/agent/operations/complete",
            {
                "Authorization": f"Bearer {self.tenant_a.agent_token}",
                "Idempotency-Key": f"{command['operation_id']}:{command['attempt']}",
            },
            {
                **identity,
                "status": "succeeded",
                "completed_at": "2026-07-22T08:01:00Z",
                "error": None,
                "evidence": {"verification": "request_removed_from_pending_list"},
            },
        )

        requests = self.store.list_friend_requests(self.tenant_a.tenant_id)
        contacts = self.store.list_channel_contacts_items(self.tenant_a.tenant_id)
        policies = self.store.list_chat_policies(
            self.tenant_a.tenant_id,
            contact_ids=[contacts[0].id],
        )
        jobs = self.store.list_operation_jobs(self.tenant_a.tenant_id)

        self.assertEqual((synced.status, limits.status, queued.status), (200, 200, 201))
        self.assertEqual((started.status, completed.status), (200, 200))
        self.assertEqual(command["operation_type"], "accept_friend")
        self.assertEqual(requests[0].status, "approved")
        self.assertEqual(contacts[0].display_name, "Alice")
        self.assertEqual(policies[0].mode, "ai_auto")
        self.assertEqual([job.operation_type for job in jobs], ["accept_friend", "send_welcome"])
        self.assertEqual([job.status for job in jobs], ["succeeded", "queued"])
        self.assertEqual(jobs[1].depends_on_job_id, jobs[0].id)
        self.assertEqual(jobs[1].payload_json["conversation_external_id"], "Alice")


if __name__ == "__main__":
    unittest.main()
