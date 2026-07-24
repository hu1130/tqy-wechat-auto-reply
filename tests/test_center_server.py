from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wechat_ai.center_server import (
    CenterApplication,
    build_center_wsgi_application,
    run_center_production_server,
)


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.activation: SimpleNamespace | None = None
        self.activation_consumed = False
        self.identity = {
            "status": "verified",
            "observed_account_id": "wxid_account_a",
            "observed_nickname": "Account A",
            "account_id": "wxid_account_a",
            "nickname": "Account A",
            "observed_at": "2026-07-23T08:00:00+00:00",
            "verified_at": "2026-07-23T08:00:01+00:00",
        }
        self.identity_fingerprint = "existing-fingerprint"
        self.identity_confirmation_key = ""

    def _record(self, name: str, values: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, values))
        return {"id": f"{name}-1", **values}

    def healthcheck(self) -> bool:
        return True

    def heartbeat_agent(self, **values: object) -> dict[str, object]:
        return self._record("heartbeat", values)

    def observe_channel_wechat_identity(self, **values: object) -> tuple[dict[str, object], dict[str, object]]:
        self.calls.append(("observe_identity", values))
        identity = dict(values["identity"])
        fingerprint = str(values["identity_fingerprint"])
        if not identity:
            status = "unavailable"
        elif not self.identity_fingerprint:
            status = "pending"
        elif fingerprint == self.identity_fingerprint:
            status = "verified"
        else:
            status = "mismatch"
        if identity:
            self.identity.update(identity)
            self.identity["observed_account_id"] = identity.get("account_id", "")
            self.identity["observed_nickname"] = identity.get("nickname", "")
            self.identity_fingerprint = (
                self.identity_fingerprint if status == "mismatch" else fingerprint
            )
        self.identity["status"] = status
        return {"id": values["channel_account_id"]}, dict(self.identity)

    def get_channel_wechat_identity(self, tenant_id: str, channel_account_id: str) -> dict[str, object]:
        self.calls.append(("get_identity", {"tenant_id": tenant_id, "channel_account_id": channel_account_id}))
        return dict(self.identity)

    def channel_wechat_identity_metadata(self, account: object) -> dict[str, object]:
        return dict(self.identity)

    def confirm_channel_wechat_identity(self, tenant_id: str, channel_account_id: str, actor_user_id: str, idempotency_key: str) -> tuple[dict[str, object], dict[str, object], bool]:
        self.calls.append(("confirm_identity", {"tenant_id": tenant_id, "channel_account_id": channel_account_id, "actor_user_id": actor_user_id, "idempotency_key": idempotency_key}))
        if self.identity_confirmation_key == idempotency_key:
            return {"id": channel_account_id, "tenant_id": tenant_id}, dict(self.identity), False
        if self.identity["status"] not in {"pending", "mismatch"}:
            raise ValueError("wechat identity is not pending confirmation")
        self.identity_confirmation_key = idempotency_key
        self.identity["status"] = "verified"
        self.identity["verified_at"] = "2026-07-23T08:00:01+00:00"
        return {"id": channel_account_id, "tenant_id": tenant_id}, dict(self.identity), True

    def record_inbound_message(self, **values: object) -> dict[str, object]:
        return self._record("message", values)

    def claim_deliveries(self, **values: object) -> list[dict[str, object]]:
        self.calls.append(("claim", values))
        return [
            SimpleNamespace(
                id="delivery-1",
                lease_token="lease-1",
                attempts=1,
                payload_json={
                    "conversation_external_id": "customer-a",
                    "content": "reply",
                },
            )
        ]

    def start_delivery(self, **values: object) -> dict[str, object]:
        return self._record("start", values)

    def complete_delivery(self, **values: object) -> dict[str, object]:
        return self._record("complete", values)

    def sync_channel_contacts(self, tenant_id: str, channel_account_id: str, contacts: object, observed_at: object) -> list[object]:
        self.calls.append(("sync_contacts", {"tenant_id": tenant_id, "channel_account_id": channel_account_id, "contacts": contacts, "observed_at": observed_at}))
        return []

    def sync_friend_requests(self, tenant_id: str, channel_account_id: str, requests: object, observed_at: object) -> list[object]:
        self.calls.append(("sync_friend_requests", {"tenant_id": tenant_id, "channel_account_id": channel_account_id, "requests": requests, "observed_at": observed_at}))
        return []

    def claim_operation_jobs(self, tenant_id: str, agent_id: str, channel_account_id: str, *, limit: int) -> list[SimpleNamespace]:
        self.calls.append(("claim_operations", {"tenant_id": tenant_id, "agent_id": agent_id, "channel_account_id": channel_account_id, "limit": limit}))
        return [SimpleNamespace(id="operation-1", lease_token="operation-lease-1", attempts=1, operation_type="accept_friend", payload_json={"external_request_id": "wxfr_1"})]

    def start_operation_job(self, *args: object) -> dict[str, object]:
        self.calls.append(("start_operation", {"args": args}))
        return {}

    def complete_operation_job(self, *args: object, **values: object) -> dict[str, object]:
        self.calls.append(("complete_operation", {"args": args, **values}))
        return {}

    @staticmethod
    def _operation(status: str = "queued") -> SimpleNamespace:
        return SimpleNamespace(
            id="operation-1", tenant_id="tenant-a", channel_account_id="channel-a",
            friend_request_id="friend-1", depends_on_job_id=None,
            operation_type="accept_friend", status=status,
            payload_json={"external_request_id": "wxfr_1"}, attempts=0,
            available_at=None, claimed_at=None, started_at=None, completed_at=None,
            error="", created_at=None, updated_at=None,
        )

    def list_channel_contacts_items(self, tenant_id: str, **values: object) -> list[SimpleNamespace]:
        self.calls.append(("list_contacts", {"tenant_id": tenant_id, **values}))
        return [SimpleNamespace(id="contact-1", tenant_id=tenant_id, channel_account_id="channel-a", external_contact_id="wxc_1", kind="friend", display_name="Alice", remark="", status="active", metadata_json={}, first_observed_at=None, last_observed_at=None, created_at=None, updated_at=None)]

    def list_chat_policies(self, tenant_id: str, *, contact_ids: list[str]) -> list[SimpleNamespace]:
        return [SimpleNamespace(id="policy-1", tenant_id=tenant_id, contact_id="contact-1", mode="ai_auto", welcome_enabled=True)]

    def set_chat_policy(self, *args: object) -> dict[str, object]:
        self.calls.append(("set_policy", {"args": args}))
        return {"id": "policy-1", "contact_id": "contact-1", "mode": args[2], "welcome_enabled": args[3]}

    def batch_set_chat_policies(self, *args: object) -> list[dict[str, object]]:
        self.calls.append(("batch_policy", {"args": args}))
        return []

    def list_friend_requests(self, tenant_id: str, **values: object) -> list[dict[str, object]]:
        self.calls.append(("list_friend_requests", {"tenant_id": tenant_id, **values}))
        return [{"id": "friend-1", "tenant_id": tenant_id, "status": "pending"}]

    def enqueue_friend_request_operations(self, tenant_id: str, channel_account_id: str, **values: object) -> list[SimpleNamespace]:
        self.calls.append(("enqueue_operations", {"tenant_id": tenant_id, "channel_account_id": channel_account_id, **values}))
        return [self._operation()]

    def list_operation_jobs(self, tenant_id: str, **values: object) -> list[SimpleNamespace]:
        return [self._operation()]

    def cancel_operation_job(self, *args: object) -> SimpleNamespace:
        self.calls.append(("cancel_operation", {"args": args}))
        return self._operation("cancelled")

    def retry_operation_job(self, *args: object) -> SimpleNamespace:
        self.calls.append(("retry_operation", {"args": args}))
        return self._operation("queued")

    def resolve_operation_job(self, *args: object) -> SimpleNamespace:
        self.calls.append(("resolve_operation", {"args": args}))
        return self._operation("succeeded")

    def get_operation_limit(self, tenant_id: str, channel_account_id: str) -> dict[str, object]:
        return {"id": "limits-1", "tenant_id": tenant_id, "channel_account_id": channel_account_id, "batch_size": 10, "min_interval_seconds": 5, "max_per_hour": 30, "max_per_day": 100, "paused": False, "welcome_message": "Welcome", "auto_enable_reply": True}

    def update_operation_limit(self, tenant_id: str, channel_account_id: str, **values: object) -> dict[str, object]:
        self.calls.append(("update_limits", {"tenant_id": tenant_id, "channel_account_id": channel_account_id, **values}))
        return self.get_operation_limit(tenant_id, channel_account_id)

    def list_messages(self, **values: object) -> list[dict[str, object]]:
        self.calls.append(("list_messages", values))
        return [{"tenant_id": values["tenant_id"], "content": "question"}]

    def get_tenant(self, tenant_id: str) -> dict[str, object]:
        return {"id": tenant_id, "slug": "tenant-a", "name": "Tenant A", "status": "active"}

    def list_tenant_members(self, tenant_id: str, *, limit: int) -> list[dict[str, object]]:
        self.calls.append(("list_members", {"tenant_id": tenant_id, "limit": limit}))
        return [{"tenant_id": tenant_id, "email": "owner@example.com", "role": "owner"}]

    def list_channel_accounts(self, tenant_id: str, *, limit: int) -> list[dict[str, object]]:
        self.calls.append(("list_channels", {"tenant_id": tenant_id, "limit": limit}))
        return [{
            "tenant_id": tenant_id,
            "id": "channel-a",
            "channel": "wechat_desktop",
            "config_json": {
                "reply_mode": "managed",
                "_center_wechat_identity_v1": {"verified_fingerprint": "secret"},
            },
        }]

    def list_agent_instances(self, tenant_id: str, *, limit: int) -> list[dict[str, object]]:
        self.calls.append(("list_agents", {"tenant_id": tenant_id, "limit": limit}))
        return [{"tenant_id": tenant_id, "id": "agent-a", "status": "online"}]

    def create_agent_activation(self, **values: object) -> tuple[SimpleNamespace, bool]:
        self.calls.append(("create_activation", values))
        if self.activation is not None:
            return self.activation, False
        created_at = datetime.now(timezone.utc)
        self.activation = SimpleNamespace(
            id="activation-1",
            tenant_id=values["tenant_id"],
            agent_id=values["agent_id"],
            channel_account_id=values["channel_account_id"],
            expires_in_seconds=values["expires_in_seconds"],
            expires_at=created_at + timedelta(seconds=int(values["expires_in_seconds"])),
            consumed_at=None,
            created_at=created_at,
        )
        return self.activation, True

    def consume_agent_activation(self, **values: object) -> tuple[SimpleNamespace, SimpleNamespace]:
        self.calls.append(("consume_activation", values))
        if self.activation_consumed:
            raise ValueError("activation_failed")
        self.activation_consumed = True
        return (
            SimpleNamespace(
                id="activation-1",
                tenant_id="tenant-a",
                agent_id="agent-a",
                channel_account_id="channel-a",
            ),
            SimpleNamespace(id="agent-a"),
        )

    def list_delivery_jobs(self, **values: object) -> list[dict[str, object]]:
        self.calls.append(("list_deliveries", values))
        return []

    def enqueue_delivery(self, **values: object) -> dict[str, object]:
        return self._record("enqueue", values)

    def get_conversation(self, tenant_id: str, conversation_id: str) -> SimpleNamespace:
        self.calls.append(("conversation", {"tenant_id": tenant_id, "id": conversation_id}))
        return SimpleNamespace(
            id=conversation_id,
            channel_account_id="channel-a",
            external_conversation_id="customer-a",
        )


class FakeActivationSecurity:
    def derive_activation_code(
        self,
        tenant_id: str,
        idempotency_key: str,
        agent_id: str,
        channel_account_id: str,
        expires_in_seconds: int,
    ) -> str:
        return "wxact_unit-test-code"

    def hash_activation_code(self, activation_code: str) -> str:
        if not activation_code.startswith("wxact_"):
            raise ValueError("activation_failed")
        return "activation-code-hash"

    def hash_device_id(self, device_id: str) -> str:
        return "device-id-hash"

    def hash_wechat_identity(self, canonical_identity: str) -> str:
        return f"wechat:{canonical_identity}"

    def issue_agent_token(self) -> SimpleNamespace:
        return SimpleNamespace(
            secret="agent_token_new-secret",
            record=SimpleNamespace(
                key_id="new-key-id",
                prefix="agent_token_",
                key_hash="new-token-hash",
            ),
        )


class CenterApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.tenant_auth: list[tuple[str, str, str]] = []
        self.agent_auth: list[tuple[str, str, str]] = []
        def authenticate_tenant(tenant: str, token: str, permission: str) -> SimpleNamespace:
            self.tenant_auth.append((tenant, token, permission))
            return SimpleNamespace(subject_id="user-a", subject_type="user", role="owner")

        self.app = CenterApplication(
            self.store,
            authenticate_tenant,
            lambda tenant, agent, channel, token: self.agent_auth.append(
                (tenant, agent, channel, token)
            ),
            activation_security=FakeActivationSecurity(),
        )

    def test_health_is_public(self) -> None:
        response = self.app.dispatch("GET", "/health", {}, {})

        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["ok"])

    def test_readyz_requires_all_runtime_checks(self) -> None:
        self.app.readiness_probe = lambda: {
            "ok": False,
            "checks": {"database": True, "migrations": {"at_head": True}, "worker": {"ok": False}},
        }

        unavailable = self.app.dispatch("GET", "/readyz", {}, {})
        self.app.readiness_probe = lambda: {
            "ok": True,
            "checks": {"database": True, "migrations": {"at_head": True}, "worker": {"ok": True}},
        }
        ready = self.app.dispatch("GET", "/readyz", {}, {})

        self.assertEqual(unavailable.status, 503)
        self.assertEqual(unavailable.body["status"], "not_ready")
        self.assertEqual(ready.status, 200)
        self.assertEqual(ready.body["status"], "ready")

    def test_wechat_identity_heartbeat_confirmation_and_operation_gate(self) -> None:
        self.store.identity["status"] = "unavailable"
        self.store.identity_fingerprint = ""
        heartbeat = SimpleNamespace(
            tenant_id="tenant-a",
            agent_id="agent-a",
            channel_account_id="channel-a",
            status="online",
            observed_at="2026-07-23T08:00:00Z",
            wechat_identity={"account_id": "wxid_account_a", "nickname": "Account A"},
        )
        with patch(
            "wechat_ai.center_server.AgentHeartbeat.from_dict",
            return_value=heartbeat,
        ):
            observed = self.app.dispatch(
                "POST",
                "/v1/agent/heartbeat",
                {"Authorization": "Bearer agent"},
                {},
            )

        blocked = self.app.dispatch(
            "POST",
            "/v1/agent/inbound",
            {"Authorization": "Bearer agent", "Idempotency-Key": "msg-pending"},
            {
                "tenant_id": "tenant-a",
                "agent_id": "agent-a",
                "channel_account_id": "channel-a",
                "external_message_id": "msg-pending",
                "conversation_external_id": "customer-a",
                "sender_external_id": "customer-a",
                "message_type": "text",
                "content": "hello",
                "occurred_at": "2026-07-23T08:00:01Z",
            },
        )
        blocked_message_count = sum(
            1 for name, _ in self.store.calls if name == "message"
        )
        headers = {
            "Authorization": "Bearer tenant-key",
            "X-Tenant-ID": "tenant-a",
            "Idempotency-Key": "confirm-channel-a",
        }
        confirmed = self.app.dispatch(
            "POST",
            "/v1/channel-accounts/channel-a/confirm-wechat-identity",
            headers,
            {},
        )
        repeated = self.app.dispatch(
            "POST",
            "/v1/channel-accounts/channel-a/confirm-wechat-identity",
            headers,
            {},
        )
        accepted = self.app.dispatch(
            "POST",
            "/v1/agent/inbound",
            {"Authorization": "Bearer agent", "Idempotency-Key": "msg-verified"},
            {
                "tenant_id": "tenant-a",
                "agent_id": "agent-a",
                "channel_account_id": "channel-a",
                "external_message_id": "msg-verified",
                "conversation_external_id": "customer-a",
                "sender_external_id": "customer-a",
                "message_type": "text",
                "content": "hello",
                "occurred_at": "2026-07-23T08:00:02Z",
            },
        )

        self.assertEqual(observed.status, 200)
        self.assertEqual(observed.body["identity_status"], "pending")
        self.assertFalse(observed.body["operations_allowed"])
        self.assertEqual(
            observed.body["wechat_identity"]["account_id"],
            "wxid_account_a",
        )
        self.assertEqual(
            set(observed.body["wechat_identity"]), {"account_id", "nickname"}
        )
        self.assertEqual(blocked.status, 409)
        self.assertEqual(blocked.body["error"], "wechat_identity_not_verified")
        self.assertEqual(blocked_message_count, 0)
        self.assertEqual((confirmed.status, repeated.status), (200, 200))
        self.assertEqual(confirmed.body["wechat_identity"]["status"], "verified")
        self.assertEqual(accepted.status, 200)
        self.assertTrue(any(name == "message" for name, _ in self.store.calls))

    def test_heartbeat_without_wechat_identity_is_unavailable(self) -> None:
        response = self.app.dispatch(
            "POST",
            "/v1/agent/heartbeat",
            {"Authorization": "Bearer agent"},
            {
                "tenant_id": "tenant-a",
                "agent_id": "agent-a",
                "channel_account_id": "channel-a",
                "status": "online",
                "observed_at": "2026-07-23T08:00:00Z",
                "capabilities": {
                    "updater": {
                        "status": "rolled_back",
                        "running_version": "1.0.0",
                        "failed_version": "2.0.0",
                        "failure_code": "candidate_health_failed",
                    }
                },
            },
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["identity_status"], "unavailable")
        self.assertFalse(response.body["operations_allowed"])
        self.assertEqual(response.body["wechat_identity"], {})
        heartbeat_call = next(
            values for name, values in self.store.calls if name == "heartbeat"
        )
        self.assertEqual(
            heartbeat_call["capabilities"]["updater"]["failure_code"],
            "candidate_health_failed",
        )

    def test_wechat_identity_confirmation_requires_user_principal(self) -> None:
        self.store.identity["status"] = "pending"
        denied_app = CenterApplication(
            self.store,
            lambda tenant, token, permission: SimpleNamespace(
                subject_id="agent-a", subject_type="agent", role="agent"
            ),
            lambda tenant, agent, channel, token: None,
            activation_security=FakeActivationSecurity(),
        )

        denied = denied_app.dispatch(
            "POST",
            "/v1/channel-accounts/channel-a/confirm-wechat-identity",
            {
                "Authorization": "Bearer agent-key",
                "X-Tenant-ID": "tenant-a",
                "Idempotency-Key": "not-a-user",
            },
            {},
        )

        self.assertEqual(denied.status, 403)
        self.assertFalse(any(name == "confirm_identity" for name, _ in self.store.calls))

    def test_wsgi_adapter_serves_json_and_rejects_invalid_json(self) -> None:
        wsgi = build_center_wsgi_application(self.app)

        def invoke(method: str, path: str, body: bytes = b"") -> tuple[str, dict[str, str], dict[str, object]]:
            captured: dict[str, object] = {}

            def start_response(status: str, headers: list[tuple[str, str]]) -> None:
                captured["status"] = status
                captured["headers"] = dict(headers)

            result = b"".join(
                wsgi(
                    {
                        "REQUEST_METHOD": method,
                        "PATH_INFO": path,
                        "QUERY_STRING": "",
                        "CONTENT_LENGTH": str(len(body)),
                        "wsgi.input": BytesIO(body),
                    },
                    start_response,
                )
            )
            return (
                str(captured["status"]),
                dict(captured["headers"]),
                json.loads(result.decode("utf-8")),
            )

        status, headers, payload = invoke("GET", "/health")
        invalid_status, _, invalid = invoke("POST", "/v1/agent/inbound", b"{")

        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(payload["ok"])
        self.assertEqual(invalid_status, "400 Bad Request")
        self.assertEqual(invalid["error"], "invalid_json")

    def test_wsgi_adapter_serves_admin_assets_with_browser_security_headers(self) -> None:
        wsgi = build_center_wsgi_application(self.app)
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured["status"] = status
            captured["headers"] = dict(headers)

        body = b"".join(
            wsgi(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/admin",
                    "QUERY_STRING": "",
                    "wsgi.input": BytesIO(),
                },
                start_response,
            )
        )

        headers = dict(captured["headers"])
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("login-form", body.decode("utf-8"))

    def test_production_server_uses_waitress_with_bounded_request_body(self) -> None:
        with patch("waitress.serve") as serve:
            run_center_production_server(
                self.app,
                host="127.0.0.1",
                port=9876,
                threads=4,
            )

        options = serve.call_args.kwargs
        self.assertEqual(options["host"], "127.0.0.1")
        self.assertEqual(options["port"], 9876)
        self.assertEqual(options["threads"], 4)
        self.assertEqual(options["max_request_body_size"], 1024 * 1024)
        self.assertFalse(options["expose_tracebacks"])
        with self.assertRaisesRegex(ValueError, "at least 4"):
            run_center_production_server(self.app, threads=3)

    def test_agent_inbound_is_tenant_scoped_and_idempotent(self) -> None:
        response = self.app.dispatch(
            "POST",
            "/v1/agent/inbound",
            {"Authorization": "Bearer agent-secret", "Idempotency-Key": "msg-1"},
            {
                "tenant_id": "tenant-a",
                "agent_id": "agent-a",
                "channel_account_id": "channel-a",
                "external_message_id": "msg-1",
                "conversation_external_id": "customer-a",
                "sender_external_id": "customer-a",
                "message_type": "text",
                "content": "hello",
                "occurred_at": "2026-07-21T08:01:00Z",
            },
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.agent_auth,
            [("tenant-a", "agent-a", "channel-a", "agent-secret")],
        )
        message_call = next(
            values for name, values in self.store.calls if name == "message"
        )
        self.assertEqual(message_call["tenant_id"], "tenant-a")
        self.assertEqual(message_call["external_message_id"], "msg-1")
        self.assertEqual(response.body, {"accepted": True})

    def test_agent_claim_and_complete_keep_agent_identity(self) -> None:
        claim = self.app.dispatch(
            "POST",
            "/v1/agent/deliveries/claim",
            {"Authorization": "Bearer token"},
            {
                "tenant_id": "tenant-a",
                "agent_id": "agent-a",
                "channel_account_id": "channel-a",
                "limit": 5,
            },
        )
        complete = self.app.dispatch(
            "POST",
            "/v1/agent/deliveries/start",
            {
                "Authorization": "Bearer token",
                "Idempotency-Key": "delivery-1:1:start",
            },
            {
                "tenant_id": "tenant-a",
                "agent_id": "agent-a",
                "channel_account_id": "channel-a",
                "delivery_id": "delivery-1",
                "lease_token": "lease-1",
                "attempt": 1,
            },
        )
        result = self.app.dispatch(
            "POST",
            "/v1/agent/deliveries/complete",
            {"Authorization": "Bearer token", "Idempotency-Key": "delivery-1:1"},
            {
                "tenant_id": "tenant-a",
                "agent_id": "agent-a",
                "channel_account_id": "channel-a",
                "delivery_id": "delivery-1",
                "lease_token": "lease-1",
                "attempt": 1,
                "status": "sent",
                "completed_at": "2026-07-21T08:02:00Z",
                "error": None,
            },
        )

        self.assertEqual(claim.status, 200)
        self.assertEqual(claim.body["deliveries"][0]["delivery_id"], "delivery-1")
        self.assertEqual(complete.status, 200)
        self.assertEqual(complete.body, {"accepted": True, "send_allowed": True})
        self.assertEqual(result.status, 200)
        self.assertEqual(self.store.calls[-1][1]["delivery_id"], "delivery-1")

    def test_agent_sync_and_operation_routes_are_bound_and_idempotent(self) -> None:
        headers = {"Authorization": "Bearer agent", "Idempotency-Key": "snapshot-1"}
        contacts = self.app.dispatch(
            "POST",
            "/v1/agent/contacts/sync",
            headers,
            {
                "tenant_id": "tenant-a", "agent_id": "agent-a", "channel_account_id": "channel-a",
                "snapshot_id": "snapshot-1", "observed_at": "2026-07-21T08:00:00Z",
                "contacts": [{"external_contact_id": "wxc_1", "display_name": "Alice", "remark": "", "kind": "friend", "status": "active", "raw": {"source": "sessions"}}],
            },
        )
        friend_headers = {"Authorization": "Bearer agent", "Idempotency-Key": "snapshot-2"}
        requests = self.app.dispatch(
            "POST",
            "/v1/agent/friend-requests/sync",
            friend_headers,
            {
                "tenant_id": "tenant-a", "agent_id": "agent-a", "channel_account_id": "channel-a",
                "snapshot_id": "snapshot-2", "observed_at": "2026-07-21T08:00:00Z",
                "requests": [{"external_request_id": "wxfr_1", "display_name": "Bob", "verification_message": "hello", "requested_at": "2026-07-21T07:59:00Z", "status": "pending", "raw": {}}],
            },
        )
        claim = self.app.dispatch(
            "POST", "/v1/agent/operations/claim", {"Authorization": "Bearer agent"},
            {"tenant_id": "tenant-a", "agent_id": "agent-a", "channel_account_id": "channel-a", "limit": 1},
        )
        start = self.app.dispatch(
            "POST", "/v1/agent/operations/start",
            {"Authorization": "Bearer agent", "Idempotency-Key": "operation-1:1:start"},
            {"tenant_id": "tenant-a", "agent_id": "agent-a", "channel_account_id": "channel-a", "operation_id": "operation-1", "lease_token": "operation-lease-1", "attempt": 1},
        )
        complete = self.app.dispatch(
            "POST", "/v1/agent/operations/complete",
            {"Authorization": "Bearer agent", "Idempotency-Key": "operation-1:1"},
            {"tenant_id": "tenant-a", "agent_id": "agent-a", "channel_account_id": "channel-a", "operation_id": "operation-1", "lease_token": "operation-lease-1", "attempt": 1, "status": "succeeded", "completed_at": "2026-07-21T08:01:00Z", "error": None, "evidence": {"verified": True}},
        )

        self.assertEqual((contacts.status, requests.status, start.status, complete.status), (200, 200, 200, 200))
        self.assertEqual(claim.body["operations"][0]["operation_type"], "accept_friend")
        sync_call = next(values for name, values in self.store.calls if name == "sync_contacts")
        self.assertEqual(sync_call["contacts"][0]["metadata"], {"source": "sessions"})
        friend_call = next(values for name, values in self.store.calls if name == "sync_friend_requests")
        self.assertEqual(friend_call["requests"][0]["requester_external_id"], "wxfr_1")

    def test_agent_snapshot_rejects_wrong_idempotency_key_before_store(self) -> None:
        response = self.app.dispatch(
            "POST", "/v1/agent/contacts/sync",
            {"Authorization": "Bearer agent", "Idempotency-Key": "wrong"},
            {"tenant_id": "tenant-a", "agent_id": "agent-a", "channel_account_id": "channel-a", "snapshot_id": "right", "observed_at": "2026-07-21T08:00:00Z", "contacts": []},
        )

        self.assertEqual(response.status, 400)
        self.assertFalse(any(name == "sync_contacts" for name, _ in self.store.calls))

    def test_tenant_message_list_requires_permission_and_tenant_header(self) -> None:
        response = self.app.dispatch(
            "GET",
            "/v1/messages?limit=20",
            {"Authorization": "Bearer tenant-key", "X-Tenant-ID": "tenant-a"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(self.tenant_auth, [("tenant-a", "tenant-key", "messages.read")])
        self.assertEqual(response.body["messages"][0]["tenant_id"], "tenant-a")

    def test_management_inventory_routes_are_tenant_scoped_and_permissioned(self) -> None:
        headers = {"Authorization": "Bearer tenant-key", "X-Tenant-ID": "tenant-a"}

        session = self.app.dispatch("GET", "/v1/session", headers)
        tenant = self.app.dispatch("GET", "/v1/tenant", headers)
        members = self.app.dispatch("GET", "/v1/members?limit=10", headers)
        channels = self.app.dispatch("GET", "/v1/channel-accounts?limit=10", headers)
        agents = self.app.dispatch("GET", "/v1/agents?limit=10", headers)

        self.assertEqual(session.body["session"], {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "role": "owner",
        })
        self.assertEqual(tenant.body["tenant"]["id"], "tenant-a")
        self.assertEqual(members.body["members"][0]["email"], "owner@example.com")
        self.assertEqual(channels.body["channel_accounts"][0]["id"], "channel-a")
        self.assertEqual(
            channels.body["channel_accounts"][0]["wechat_identity"]["status"],
            "verified",
        )
        self.assertEqual(
            channels.body["channel_accounts"][0]["wechat_identity"]["observed_nickname"],
            "Account A",
        )
        self.assertNotIn(
            "_center_wechat_identity_v1",
            channels.body["channel_accounts"][0]["config_json"],
        )
        self.assertNotIn("secret", str(channels.body))
        self.assertEqual(agents.body["agents"][0]["id"], "agent-a")
        self.assertIn(("tenant-a", "tenant-key", "members.manage"), self.tenant_auth)
        self.assertIn(("tenant-a", "tenant-key", "channels.manage"), self.tenant_auth)
        self.assertIn(("tenant-a", "tenant-key", "agents.manage"), self.tenant_auth)

    def test_tenant_creates_idempotent_agent_activation_without_persisting_code(self) -> None:
        headers = {
            "Authorization": "Bearer tenant-key",
            "X-Tenant-ID": "tenant-a",
            "Idempotency-Key": "activate-agent-a",
        }
        payload = {
            "agent_id": "agent-a",
            "channel_account_id": "channel-a",
            "expires_in_seconds": 900,
        }

        created = self.app.dispatch("POST", "/v1/agent-activations", headers, payload)
        repeated = self.app.dispatch("POST", "/v1/agent-activations", headers, payload)

        self.assertEqual(created.status, 201)
        self.assertEqual(repeated.status, 200)
        self.assertEqual(created.body, repeated.body)
        self.assertEqual(created.body["activation_code"], "wxact_unit-test-code")
        self.assertEqual(created.body["activation"]["agent_id"], "agent-a")
        self.assertNotIn("code_hash", created.body["activation"])
        self.assertNotIn("agent_token", str(created.body["activation"]))
        self.assertIn(("tenant-a", "tenant-key", "agents.manage"), self.tenant_auth)
        create_call = next(
            values for name, values in self.store.calls if name == "create_activation"
        )
        self.assertEqual(create_call["code_hash"], "activation-code-hash")
        self.assertNotIn("wxact_unit-test-code", str(create_call))

    def test_public_agent_activation_consumes_once_and_fails_closed_on_replay(self) -> None:
        payload = {
            "activation_code": "wxact_unit-test-code",
            "device_id": "device-a",
            "device_name": "Reception PC",
            "agent_version": "1.0.0",
        }

        activated = self.app.dispatch("POST", "/v1/agent/activate", {}, payload)
        replayed_from_other_device = self.app.dispatch(
            "POST",
            "/v1/agent/activate",
            {},
            {**payload, "device_id": "device-b"},
        )

        self.assertEqual(
            activated.body,
            {
                "tenant_id": "tenant-a",
                "agent_instance_id": "agent-a",
                "channel_account_id": "channel-a",
                "agent_token": "agent_token_new-secret",
            },
        )
        self.assertEqual(replayed_from_other_device.status, 400)
        self.assertEqual(replayed_from_other_device.body["error"], "activation_failed")
        self.assertNotIn("agent_token_new-secret", str(replayed_from_other_device.body))
        self.assertEqual(self.agent_auth, [])
        consume_call = next(
            values for name, values in self.store.calls if name == "consume_activation"
        )
        self.assertEqual(consume_call["device_hash"], "device-id-hash")
        self.assertNotIn(payload["activation_code"], str(consume_call))
        self.assertNotIn(payload["device_id"], str(consume_call))
        self.assertNotIn("agent_token_new-secret", str(consume_call))

    def test_agent_activation_requires_permission_and_valid_expiry(self) -> None:
        denied_app = CenterApplication(
            self.store,
            lambda tenant, token, permission: (_ for _ in ()).throw(PermissionError()),
            lambda tenant, agent, channel, token: None,
            activation_security=FakeActivationSecurity(),
        )
        headers = {
            "Authorization": "Bearer tenant-key",
            "X-Tenant-ID": "tenant-a",
            "Idempotency-Key": "activation-denied",
        }
        payload = {
            "agent_id": "agent-a",
            "channel_account_id": "channel-a",
            "expires_in_seconds": 299,
        }

        denied = denied_app.dispatch("POST", "/v1/agent-activations", headers, payload)
        invalid_expiry = self.app.dispatch(
            "POST",
            "/v1/agent-activations",
            {**headers, "Idempotency-Key": "activation-invalid-expiry"},
            payload,
        )

        self.assertEqual(denied.status, 403)
        self.assertEqual(invalid_expiry.status, 400)
        self.assertNotIn("activation_code", str(denied.body))
        self.assertNotIn("activation_code", str(invalid_expiry.body))

    def test_contact_policy_and_friend_queue_routes_enforce_scope_and_fields(self) -> None:
        headers = {"Authorization": "Bearer key", "X-Tenant-ID": "tenant-a"}
        contacts = self.app.dispatch("GET", "/v1/contacts?channel_account_id=channel-a", headers)
        update = self.app.dispatch(
            "PUT", "/v1/contacts/contact-1/policy",
            {**headers, "Idempotency-Key": "policy-1"},
            {"policy": "blocked", "welcome_enabled": False},
        )
        queued = self.app.dispatch(
            "POST", "/v1/friend-requests/approve-selected",
            {**headers, "Idempotency-Key": "friend-action-1"},
            {"channel_account_id": "channel-a", "friend_request_ids": ["friend-1"], "send_welcome_message": True, "welcome_message": "Welcome", "auto_enable_reply": True},
        )

        self.assertEqual(contacts.body["contacts"][0]["policy"], "ai_auto")
        self.assertTrue(contacts.body["contacts"][0]["welcome_enabled"])
        self.assertEqual(update.status, 200)
        self.assertEqual(queued.status, 201)
        enqueue = next(values for name, values in self.store.calls if name == "enqueue_operations")
        self.assertEqual(enqueue["selection"], "selected")
        self.assertEqual(enqueue["action"], "accept_friend")
        self.assertEqual(enqueue["welcome_message"], "Welcome")

    def test_friend_welcome_and_operation_limit_validation_fail_closed(self) -> None:
        headers = {"Authorization": "Bearer key", "X-Tenant-ID": "tenant-a", "Idempotency-Key": "action-1"}
        missing_welcome = self.app.dispatch(
            "POST", "/v1/friend-requests/approve-batch", headers,
            {"channel_account_id": "channel-a", "batch_size": 10, "send_welcome_message": True, "auto_enable_reply": False},
        )
        bad_limit = self.app.dispatch(
            "PUT", "/v1/operation-limits/channel-a", headers,
            {"batch_size": True},
        )

        self.assertEqual(missing_welcome.status, 400)
        self.assertEqual(bad_limit.status, 400)

    def test_operation_jobs_and_limits_are_tenant_permissioned(self) -> None:
        headers = {"Authorization": "Bearer key", "X-Tenant-ID": "tenant-a"}
        jobs = self.app.dispatch("GET", "/v1/operation-jobs?channel_account_id=channel-a", headers)
        limits = self.app.dispatch("GET", "/v1/operation-limits?channel_account_id=channel-a", headers)
        cancel = self.app.dispatch(
            "POST", "/v1/operation-jobs/operation-1/cancel",
            {**headers, "Idempotency-Key": "cancel-1"}, {"reason": "manual"},
        )

        self.assertEqual(jobs.body["operation_jobs"][0]["status"], "queued")
        self.assertEqual(limits.body["operation_limits"]["batch_size"], 10)
        self.assertEqual(cancel.body["operation_job"]["status"], "cancelled")
        self.assertIn(("tenant-a", "key", "deliveries.manage"), self.tenant_auth)

    def test_pending_friend_request_exposes_active_operation_status(self) -> None:
        headers = {"Authorization": "Bearer key", "X-Tenant-ID": "tenant-a"}

        response = self.app.dispatch(
            "GET",
            "/v1/friend-requests?channel_account_id=channel-a&status=pending",
            headers,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["friend_requests"][0]["operation_status"], "queued")
        self.assertEqual(response.body["friend_requests"][0]["operation_job_id"], "operation-1")

    def test_enqueue_requires_idempotency_key_and_delivery_permission(self) -> None:
        missing = self.app.dispatch(
            "POST",
            "/v1/deliveries",
            {"Authorization": "Bearer key", "X-Tenant-ID": "tenant-a"},
            {"channel_account_id": "channel-a", "conversation_id": "conversation-a", "content": "reply"},
        )
        created = self.app.dispatch(
            "POST",
            "/v1/deliveries",
            {
                "Authorization": "Bearer key",
                "X-Tenant-ID": "tenant-a",
                "Idempotency-Key": "reply-1",
            },
            {"channel_account_id": "channel-a", "conversation_id": "conversation-a", "content": "reply"},
        )

        self.assertEqual(missing.status, 400)
        self.assertEqual(created.status, 201)
        self.assertIn(("tenant-a", "key", "deliveries.manage"), self.tenant_auth)
        self.assertEqual(self.store.calls[-1][1]["dedupe_key"], "reply-1")

    def test_missing_or_denied_auth_is_fail_closed(self) -> None:
        missing = self.app.dispatch("GET", "/v1/messages", {"X-Tenant-ID": "tenant-a"})
        denied_app = CenterApplication(
            self.store,
            lambda tenant, token, permission: (_ for _ in ()).throw(PermissionError()),
            lambda tenant, agent, channel, token: None,
        )
        denied = denied_app.dispatch(
            "GET",
            "/v1/messages",
            {"X-Tenant-ID": "tenant-a", "Authorization": "Bearer bad"},
        )

        self.assertEqual(missing.status, 401)
        self.assertEqual(denied.status, 403)
        self.assertNotIn("bad", str(denied.body))


if __name__ == "__main__":
    unittest.main()
