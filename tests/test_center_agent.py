from __future__ import annotations

import io
import json
import unittest
from dataclasses import replace
from urllib.error import HTTPError, URLError
from urllib.request import Request

from wechat_ai.center_agent import (
    AgentHeartbeat,
    CenterAgentClient,
    ContactSnapshot,
    ContactSnapshotItem,
    DeliveryCommand,
    DeliveryResult,
    DeliveryStart,
    FriendRequestSnapshot,
    FriendRequestSnapshotItem,
    InboundEnvelope,
    OperationCommand,
    OperationResult,
    OperationStart,
)
from wechat_ai.reliability import RetryPolicy


class FakeResponse:
    def __init__(self, payload: object = None, status: int = 200, raw: bytes | None = None) -> None:
        self.status = status
        self.raw = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self.raw if amount < 0 else self.raw[:amount]

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[Request, float]] = []

    def open(self, request: Request, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def heartbeat() -> AgentHeartbeat:
    return AgentHeartbeat(
        "tenant-1",
        "agent-1",
        "channel-1",
        "online",
        "2026-07-21T08:00:00Z",
        {"account_id": "wxid_operator", "nickname": "Operator"},
        {
            "connector": {"send_text": True},
            "updater": {"status": "healthy", "running_version": "1.0.0"},
        },
    )


def heartbeat_response(
    *,
    identity_status: str = "verified",
    operations_allowed: bool = True,
    wechat_identity: object = None,
) -> dict[str, object]:
    return {
        "accepted": True,
        "identity_status": identity_status,
        "operations_allowed": operations_allowed,
        "wechat_identity": (
            {"account_id": "wxid_operator", "nickname": "Operator"}
            if wechat_identity is None
            else wechat_identity
        ),
    }


def inbound() -> InboundEnvelope:
    return InboundEnvelope(
        tenant_id="tenant-1",
        agent_id="agent-1",
        channel_account_id="channel-1",
        external_message_id="wx-msg-100",
        conversation_external_id="wx-chat-9",
        sender_external_id="wx-user-7",
        message_type="text",
        content="你好",
        occurred_at="2026-07-21T08:01:00Z",
    )


def command(delivery_id: str = "delivery-1") -> DeliveryCommand:
    return DeliveryCommand(
        tenant_id="tenant-1",
        agent_id="agent-1",
        channel_account_id="channel-1",
        delivery_id=delivery_id,
        lease_token=f"lease-{delivery_id}",
        conversation_external_id="wx-chat-9",
        content="您好，请问有什么可以帮您？",
        status="claimed",
        attempt=1,
    )


def result() -> DeliveryResult:
    return DeliveryResult(
        tenant_id="tenant-1",
        agent_id="agent-1",
        channel_account_id="channel-1",
        delivery_id="delivery-1",
        lease_token="lease-delivery-1",
        attempt=2,
        status="sent",
        completed_at="2026-07-21T08:02:00Z",
    )


def contact_item(external_contact_id: str = "wx-contact-1") -> ContactSnapshotItem:
    return ContactSnapshotItem(
        external_contact_id=external_contact_id,
        display_name="Customer One",
        remark="priority",
        kind="friend",
        status="active",
        raw={"source": "wechat", "tags": ["vip"]},
    )


def contact_snapshot() -> ContactSnapshot:
    return ContactSnapshot(
        tenant_id="tenant-1",
        agent_id="agent-1",
        channel_account_id="channel-1",
        snapshot_id="contacts-snapshot-1",
        observed_at="2026-07-22T08:00:00Z",
        contacts=[contact_item()],
    )


def friend_request_item(external_request_id: str = "wx-request-1") -> FriendRequestSnapshotItem:
    return FriendRequestSnapshotItem(
        external_request_id=external_request_id,
        display_name="Prospect One",
        verification_message="Please add me",
        requested_at="2026-07-22T08:01:00Z",
        status="pending",
        raw={"scene": 17},
    )


def friend_request_snapshot() -> FriendRequestSnapshot:
    return FriendRequestSnapshot(
        tenant_id="tenant-1",
        agent_id="agent-1",
        channel_account_id="channel-1",
        snapshot_id="requests-snapshot-1",
        observed_at="2026-07-22T08:02:00Z",
        requests=[friend_request_item()],
    )


def operation_command(operation_id: str = "operation-1") -> OperationCommand:
    return OperationCommand(
        tenant_id="tenant-1",
        agent_id="agent-1",
        channel_account_id="channel-1",
        operation_id=operation_id,
        lease_token=f"lease-{operation_id}",
        operation_type="accept_friend",
        attempt=1,
        payload={"external_request_id": "wx-request-1", "welcome": True},
    )


def operation_start() -> OperationStart:
    return OperationStart(
        tenant_id="tenant-1",
        agent_id="agent-1",
        channel_account_id="channel-1",
        operation_id="operation-1",
        lease_token="lease-operation-1",
        attempt=2,
    )


def operation_result() -> OperationResult:
    return OperationResult(
        tenant_id="tenant-1",
        agent_id="agent-1",
        channel_account_id="channel-1",
        operation_id="operation-1",
        lease_token="lease-operation-1",
        attempt=2,
        status="succeeded",
        completed_at="2026-07-22T08:03:00Z",
        error=None,
        evidence={"matched_contact_id": "wx-contact-1"},
    )


class ProtocolModelTests(unittest.TestCase):
    def test_models_round_trip_without_changing_field_types(self) -> None:
        start = DeliveryStart(
            "tenant-1",
            "agent-1",
            "channel-1",
            "delivery-1",
            "lease-delivery-1",
            1,
        )
        models = [heartbeat(), inbound(), command(), start, result()]

        for model in models:
            rebuilt = type(model).from_dict(model.to_dict())
            self.assertEqual(rebuilt, model)
            for name, value in model.to_dict().items():
                self.assertIs(type(rebuilt.to_dict()[name]), type(value), name)

    def test_all_identity_fields_must_be_non_empty(self) -> None:
        for field in ("tenant_id", "agent_id", "channel_account_id"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    replace(heartbeat(), **{field: "  "})

        with self.assertRaisesRegex(ValueError, "external_message_id"):
            replace(inbound(), external_message_id="")
        with self.assertRaisesRegex(ValueError, "delivery_id"):
            replace(command(), delivery_id="")

    def test_message_and_delivery_content_must_be_non_empty_strings(self) -> None:
        with self.assertRaisesRegex(ValueError, "content"):
            replace(inbound(), content=" \t ")
        with self.assertRaisesRegex(ValueError, "content"):
            replace(command(), content="")
        with self.assertRaisesRegex(ValueError, "content must be a string"):
            InboundEnvelope.from_dict({**inbound().to_dict(), "content": 123})

    def test_unknown_statuses_are_rejected(self) -> None:
        for model in (heartbeat(), command(), result()):
            with self.subTest(model=type(model).__name__):
                with self.assertRaisesRegex(ValueError, "unknown status"):
                    replace(model, status="mystery")

    def test_delivery_attempt_is_a_strict_positive_integer(self) -> None:
        with self.assertRaisesRegex(ValueError, "attempt"):
            replace(command(), attempt=True)
        with self.assertRaisesRegex(ValueError, "attempt"):
            replace(result(), attempt=0)

    def test_delivery_result_error_matches_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "error must be null"):
            replace(result(), error="unexpected")
        with self.assertRaisesRegex(ValueError, "error is required"):
            replace(result(), status="failed")
        failed = replace(result(), status="failed", error="window unavailable")
        self.assertEqual(DeliveryResult.from_dict(failed.to_dict()), failed)

    def test_from_dict_rejects_missing_and_unknown_fields(self) -> None:
        missing = inbound().to_dict()
        missing.pop("external_message_id")
        with self.assertRaisesRegex(ValueError, "missing fields"):
            InboundEnvelope.from_dict(missing)

        unknown = command().to_dict()
        unknown["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            DeliveryCommand.from_dict(unknown)

        with self.assertRaisesRegex(ValueError, "field names must be strings"):
            AgentHeartbeat.from_dict({**heartbeat().to_dict(), 1: "bad-key"})

    def test_heartbeat_accepts_missing_identity_from_older_agents(self) -> None:
        payload = heartbeat().to_dict()
        payload.pop("wechat_identity")
        payload.pop("capabilities")

        rebuilt = AgentHeartbeat.from_dict(payload)

        self.assertEqual(rebuilt.wechat_identity, {})
        self.assertEqual(rebuilt.capabilities, {})
        self.assertEqual(rebuilt.to_dict()["wechat_identity"], {})
        self.assertEqual(rebuilt.to_dict()["capabilities"], {})

    def test_heartbeat_capabilities_are_bounded_json(self) -> None:
        rebuilt = AgentHeartbeat.from_dict(heartbeat().to_dict())

        self.assertEqual(
            rebuilt.capabilities["updater"],
            {"status": "healthy", "running_version": "1.0.0"},
        )
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            replace(heartbeat(), capabilities=["invalid"])  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "JSON size limit"):
            replace(
                heartbeat(),
                capabilities={
                    f"key-{index:03d}": "x" * 300 for index in range(256)
                },
            )

    def test_heartbeat_identity_rejects_unknown_invalid_and_unsafe_values(self) -> None:
        invalid_values = [
            ({"phone": "13800000000"}, "unknown fields"),
            ({"account_id": 123}, "must be a string"),
            ({"account_id": " "}, "must not be empty"),
            ({"nickname": "bad\tname"}, "control character"),
            ({"nickname": "bad\u0085name"}, "control character"),
            ({"nickname": "x" * 257}, "must not exceed 256"),
        ]
        for identity, message in invalid_values:
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(ValueError, message):
                    replace(heartbeat(), wechat_identity=identity)

        with self.assertRaisesRegex(ValueError, "unknown fields"):
            AgentHeartbeat.from_dict({**heartbeat().to_dict(), "unexpected": True})

    def test_operation_models_round_trip_with_exact_fields(self) -> None:
        models = [
            contact_item(),
            contact_snapshot(),
            friend_request_item(),
            friend_request_snapshot(),
            operation_command(),
            operation_start(),
            operation_result(),
        ]

        for model in models:
            with self.subTest(model=type(model).__name__):
                self.assertEqual(type(model).from_dict(model.to_dict()), model)

    def test_operation_protocol_models_reject_unknown_and_missing_fields(self) -> None:
        for model in (
            contact_item(),
            contact_snapshot(),
            friend_request_item(),
            friend_request_snapshot(),
            operation_command(),
            operation_start(),
            operation_result(),
        ):
            with self.subTest(model=type(model).__name__, case="unknown"):
                payload = {**model.to_dict(), "unexpected": True}
                with self.assertRaisesRegex(ValueError, "unknown fields"):
                    type(model).from_dict(payload)
            with self.subTest(model=type(model).__name__, case="missing"):
                payload = model.to_dict()
                payload.pop(next(iter(payload)))
                with self.assertRaisesRegex(ValueError, "missing fields"):
                    type(model).from_dict(payload)

    def test_snapshot_arrays_are_typed_and_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "contacts.*must be an array"):
            ContactSnapshot.from_dict({**contact_snapshot().to_dict(), "contacts": {}})
        with self.assertRaisesRegex(ValueError, "ContactSnapshotItem"):
            replace(contact_snapshot(), contacts=["not-a-contact"])  # type: ignore[list-item]
        with self.assertRaisesRegex(ValueError, "5000"):
            replace(contact_snapshot(), contacts=[contact_item()] * 5001)

        with self.assertRaisesRegex(ValueError, "requests.*must be an array"):
            FriendRequestSnapshot.from_dict(
                {**friend_request_snapshot().to_dict(), "requests": "invalid"}
            )
        with self.assertRaisesRegex(ValueError, "FriendRequestSnapshotItem"):
            replace(friend_request_snapshot(), requests=[object()])  # type: ignore[list-item]

    def test_operation_protocol_text_fields_have_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "external_contact_id"):
            replace(contact_item(), external_contact_id="x" * 257)
        with self.assertRaisesRegex(ValueError, "display_name"):
            replace(contact_item(), display_name="x" * 257)
        with self.assertRaisesRegex(ValueError, "remark"):
            replace(contact_item(), remark="x" * 1025)
        with self.assertRaisesRegex(ValueError, "verification_message"):
            replace(friend_request_item(), verification_message="x" * 4097)
        with self.assertRaisesRegex(ValueError, "operation_type"):
            replace(operation_command(), operation_type="x" * 65)
        with self.assertRaisesRegex(ValueError, "snapshot_id"):
            replace(contact_snapshot(), snapshot_id="")

    def test_snapshot_and_operation_enums_reject_unknown_values(self) -> None:
        cases = [
            (contact_item(), {"kind": "official_account"}, "kind"),
            (contact_item(), {"status": "pending"}, "status"),
            (friend_request_item(), {"status": "active"}, "status"),
            (operation_command(), {"operation_type": "run_script"}, "operation_type"),
        ]
        for model, changes, field_name in cases:
            with self.subTest(model=type(model).__name__, field=field_name):
                with self.assertRaisesRegex(ValueError, f"unknown {field_name}"):
                    replace(model, **changes)

    def test_raw_payload_and_evidence_require_bounded_json_objects(self) -> None:
        with self.assertRaisesRegex(ValueError, "raw must be a JSON object"):
            replace(contact_item(), raw=[])  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "JSON-compatible"):
            replace(operation_command(), payload={"bad": object()})
        with self.assertRaisesRegex(ValueError, "non-finite"):
            replace(operation_result(), evidence={"confidence": float("nan")})
        with self.assertRaisesRegex(ValueError, "nesting depth"):
            nested: object = "leaf"
            for _ in range(10):
                nested = {"next": nested}
            replace(friend_request_item(), raw={"nested": nested})
        with self.assertRaisesRegex(ValueError, "object keys"):
            replace(operation_command(), payload={f"key-{index}": index for index in range(257)})

    def test_operation_result_status_controls_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown status"):
            replace(operation_result(), status="sent")
        with self.assertRaisesRegex(ValueError, "error must be null"):
            replace(operation_result(), error="unexpected")
        with self.assertRaisesRegex(ValueError, "error is required"):
            replace(operation_result(), status="failed")

        failed = replace(operation_result(), status="failed", error="window not found")
        uncertain = replace(operation_result(), status="uncertain", error="result not observable")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(uncertain.status, "uncertain")


class CenterAgentClientTests(unittest.TestCase):
    def make_client(self, responses: list[FakeResponse | BaseException], **kwargs: object) -> tuple[CenterAgentClient, FakeOpener]:
        opener = FakeOpener(responses)
        kwargs.setdefault(
            "retry_policy",
            RetryPolicy(max_attempts=1, base_delay=0, max_delay=0),
        )
        client = CenterAgentClient(
            "https://center.example.test/api",
            "agent-secret-token",
            "tenant-1",
            "agent-1",
            "channel-1",
            timeout=7,
            opener=opener,
            **kwargs,
        )
        return client, opener

    def test_retryable_transport_failure_keeps_same_idempotency_key(self) -> None:
        delays: list[float] = []
        client, opener = self.make_client(
            [URLError("temporary"), FakeResponse({"accepted": True})],
            retry_policy=RetryPolicy(
                max_attempts=2,
                base_delay=0.25,
                max_delay=0.25,
                sleep=delays.append,
            ),
        )

        client.report_inbound(inbound())

        self.assertEqual(delays, [0.25])
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(
            opener.calls[0][0].get_header("Idempotency-key"),
            opener.calls[1][0].get_header("Idempotency-key"),
        )

    def test_auth_failures_do_not_open_center_availability_circuit(self) -> None:
        responses: list[FakeResponse | BaseException] = [
            FakeResponse({"error": "denied"}, status=401) for _ in range(5)
        ]
        responses.append(FakeResponse({"accepted": True}))
        client, _ = self.make_client(responses)

        for _ in range(5):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                client.report_inbound(inbound())
        client.report_inbound(inbound())

    def assert_request(self, opener: FakeOpener, suffix: str, payload: object, idempotency_key: str) -> None:
        request, timeout = opener.calls[-1]
        self.assertEqual(request.full_url, f"https://center.example.test/api{suffix}")
        self.assertEqual(request.method, "POST")
        self.assertEqual(timeout, 7)
        self.assertEqual(request.get_header("Authorization"), "Bearer agent-secret-token")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Idempotency-key"), idempotency_key)
        self.assertEqual(json.loads(request.data.decode("utf-8")), payload)

    def test_heartbeat_posts_json_and_uses_caller_stable_id(self) -> None:
        expected = heartbeat_response()
        client, opener = self.make_client([FakeResponse(expected)])
        value = heartbeat()

        self.assertEqual(client.heartbeat(value, stable_id="heartbeat-run-5"), expected)

        self.assert_request(opener, "/v1/agent/heartbeat", value.to_dict(), "heartbeat-run-5")

    def test_heartbeat_default_idempotency_key_contains_agent_and_minute_bucket(self) -> None:
        client, opener = self.make_client([FakeResponse(heartbeat_response())])

        from unittest.mock import patch

        with patch("wechat_ai.center_agent.time.time", return_value=125.0):
            client.heartbeat(heartbeat())

        request, _ = opener.calls[0]
        self.assertEqual(request.get_header("Idempotency-key"), "agent-1:2")

    def test_heartbeat_response_is_strict_and_fail_closed(self) -> None:
        cases = [
            ({"accepted": True}, "missing fields"),
            ({**heartbeat_response(), "extra": True}, "unknown fields"),
            ({**heartbeat_response(), "accepted": "yes"}, "must be a boolean"),
            (
                heartbeat_response(identity_status="pending", operations_allowed=True),
                "unverified identity",
            ),
            (heartbeat_response(wechat_identity={}), "empty WeChat identity"),
            (
                heartbeat_response(wechat_identity={"phone": "13800000000"}),
                "unknown fields",
            ),
        ]
        for payload, message in cases:
            with self.subTest(message=message):
                client, _ = self.make_client([FakeResponse(payload)])
                with self.assertRaisesRegex(RuntimeError, message):
                    client.heartbeat(heartbeat())

    def test_heartbeat_response_allows_blocked_identity_states(self) -> None:
        for identity_status in ("unavailable", "pending", "mismatch"):
            with self.subTest(identity_status=identity_status):
                expected = heartbeat_response(
                    identity_status=identity_status,
                    operations_allowed=False,
                    wechat_identity={},
                )
                client, _ = self.make_client([FakeResponse(expected)])
                self.assertEqual(client.heartbeat(heartbeat()), expected)

    def test_report_inbound_uses_external_message_id_for_idempotency(self) -> None:
        client, opener = self.make_client([FakeResponse({"accepted": True})])
        value = inbound()

        client.report_inbound(value)

        self.assert_request(opener, "/v1/agent/inbound", value.to_dict(), "wx-msg-100")

    def test_sync_contacts_uses_snapshot_id_for_idempotency(self) -> None:
        client, opener = self.make_client([FakeResponse({"accepted": True})])
        value = contact_snapshot()

        client.sync_contacts(value)

        self.assert_request(
            opener,
            "/v1/agent/contacts/sync",
            value.to_dict(),
            "contacts-snapshot-1",
        )

    def test_sync_friend_requests_uses_snapshot_id_for_idempotency(self) -> None:
        client, opener = self.make_client([FakeResponse({"accepted": True})])
        value = friend_request_snapshot()

        client.sync_friend_requests(value)

        self.assert_request(
            opener,
            "/v1/agent/friend-requests/sync",
            value.to_dict(),
            "requests-snapshot-1",
        )

    def test_snapshot_retry_keeps_snapshot_id_as_idempotency_key(self) -> None:
        client, opener = self.make_client(
            [URLError("temporary"), FakeResponse({"accepted": True})],
            retry_policy=RetryPolicy(max_attempts=2, base_delay=0, max_delay=0),
        )

        client.sync_contacts(contact_snapshot())

        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(
            [call[0].get_header("Idempotency-key") for call in opener.calls],
            ["contacts-snapshot-1", "contacts-snapshot-1"],
        )

    def test_claim_is_post_and_parses_multiple_commands(self) -> None:
        expected = [command("delivery-1"), command("delivery-2")]
        client, opener = self.make_client(
            [FakeResponse({"deliveries": [item.to_dict() for item in expected]})]
        )

        actual = client.claim_deliveries(25)

        self.assertEqual(actual, expected)
        request, _ = opener.calls[0]
        self.assertEqual(request.full_url, "https://center.example.test/api/v1/agent/deliveries/claim")
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            json.loads(request.data),
            {
                "tenant_id": "tenant-1",
                "agent_id": "agent-1",
                "channel_account_id": "channel-1",
                "limit": 25,
            },
        )
        self.assertTrue((request.get_header("Idempotency-key") or "").startswith("claim:"))

    def test_claim_limit_is_restricted_to_one_through_one_hundred(self) -> None:
        client, opener = self.make_client([])
        for value in (0, 101, True, 1.5):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "between 1 and 100"):
                    client.claim_deliveries(value)  # type: ignore[arg-type]
        self.assertEqual(opener.calls, [])

    def test_complete_delivery_uses_delivery_and_attempt_idempotency(self) -> None:
        client, opener = self.make_client([FakeResponse({"accepted": True})])
        value = result()

        client.complete_delivery(value)

        self.assert_request(opener, "/v1/agent/deliveries/complete", value.to_dict(), "delivery-1:2")

    def test_start_delivery_preserves_lease_and_is_idempotent(self) -> None:
        client, opener = self.make_client(
            [FakeResponse({"accepted": True, "send_allowed": True})]
        )
        value = DeliveryStart(
            "tenant-1",
            "agent-1",
            "channel-1",
            "delivery-1",
            "lease-delivery-1",
            2,
        )

        self.assertTrue(client.start_delivery(value))

        self.assert_request(
            opener,
            "/v1/agent/deliveries/start",
            value.to_dict(),
            "delivery-1:2:start",
        )

    def test_start_delivery_reports_policy_suppression(self) -> None:
        client, _ = self.make_client(
            [FakeResponse({"accepted": True, "send_allowed": False})]
        )

        self.assertFalse(
            client.start_delivery(
                DeliveryStart(
                    "tenant-1",
                    "agent-1",
                    "channel-1",
                    "delivery-1",
                    "lease-delivery-1",
                    2,
                )
            )
        )

    def test_claim_operations_posts_bound_identity_and_parses_commands(self) -> None:
        expected = [operation_command("operation-1"), operation_command("operation-2")]
        client, opener = self.make_client(
            [FakeResponse({"operations": [item.to_dict() for item in expected]})]
        )

        actual = client.claim_operations(10)

        self.assertEqual(actual, expected)
        request, _ = opener.calls[0]
        self.assertEqual(request.full_url, "https://center.example.test/api/v1/agent/operations/claim")
        self.assertEqual(
            json.loads(request.data),
            {
                "tenant_id": "tenant-1",
                "agent_id": "agent-1",
                "channel_account_id": "channel-1",
                "limit": 10,
            },
        )
        self.assertTrue(
            (request.get_header("Idempotency-key") or "").startswith("operations:claim:")
        )

    def test_start_and_complete_operation_use_stable_operation_keys(self) -> None:
        client, opener = self.make_client(
            [FakeResponse({"accepted": True}), FakeResponse({"accepted": True})]
        )
        start = operation_start()
        completed = operation_result()

        client.start_operation(start)
        self.assert_request(
            opener,
            "/v1/agent/operations/start",
            start.to_dict(),
            "operation-1:2:start",
        )

        client.complete_operation(completed)
        self.assert_request(
            opener,
            "/v1/agent/operations/complete",
            completed.to_dict(),
            "operation-1:2",
        )

    def test_operation_claim_limit_is_restricted(self) -> None:
        client, opener = self.make_client([])
        for value in (0, 101, True, 1.5):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "between 1 and 100"):
                    client.claim_operations(value)  # type: ignore[arg-type]
        self.assertEqual(opener.calls, [])

    def test_claim_operations_rejects_invalid_responses_and_unbound_commands(self) -> None:
        cases = [
            (FakeResponse([]), "JSON object"),
            (FakeResponse({"operations": {}}), "must be an array"),
            (
                FakeResponse({"operations": [{**operation_command().to_dict(), "extra": True}]}),
                "invalid operation command",
            ),
            (
                FakeResponse(
                    {"operations": [{**operation_command().to_dict(), "tenant_id": "tenant-2"}]}
                ),
                "configured Agent binding",
            ),
            (
                FakeResponse({"operations": [operation_command().to_dict()] * 2}),
                "requested limit",
            ),
        ]
        for response, message in cases:
            with self.subTest(message=message):
                client, _ = self.make_client([response])
                with self.assertRaisesRegex(RuntimeError, message):
                    client.claim_operations(1)

    def test_claim_operations_keeps_the_shared_response_size_limit(self) -> None:
        client, _ = self.make_client([FakeResponse(raw=b"x" * (1024 * 1024 + 1))])

        with self.assertRaisesRegex(RuntimeError, "response exceeded size limit"):
            client.claim_operations(1)

    def test_sync_and_operation_completion_reject_unaccepted_responses(self) -> None:
        cases = [
            ("sync", FakeResponse({"accepted": False})),
            ("complete", FakeResponse({"accepted": "yes"})),
        ]
        for operation, response in cases:
            with self.subTest(operation=operation):
                client, _ = self.make_client([response])
                with self.assertRaises(RuntimeError):
                    if operation == "sync":
                        client.sync_friend_requests(friend_request_snapshot())
                    else:
                        client.complete_operation(operation_result())

    def test_operation_methods_require_models_bound_to_client_identity(self) -> None:
        client, opener = self.make_client([])
        cases = [
            (client.sync_contacts, replace(contact_snapshot(), tenant_id="tenant-2")),
            (
                client.sync_friend_requests,
                replace(friend_request_snapshot(), agent_id="agent-2"),
            ),
            (client.start_operation, replace(operation_start(), channel_account_id="channel-2")),
            (client.complete_operation, replace(operation_result(), tenant_id="tenant-2")),
        ]
        for method, value in cases:
            with self.subTest(method=method.__name__):
                with self.assertRaisesRegex(ValueError, "configured Agent binding"):
                    method(value)
        self.assertEqual(opener.calls, [])

    def test_operation_methods_reject_wrong_model_types(self) -> None:
        client, opener = self.make_client([])
        cases = [
            (client.sync_contacts, friend_request_snapshot(), "ContactSnapshot"),
            (client.sync_friend_requests, contact_snapshot(), "FriendRequestSnapshot"),
            (client.start_operation, operation_result(), "OperationStart"),
            (client.complete_operation, operation_start(), "OperationResult"),
        ]
        for method, value, model_name in cases:
            with self.subTest(method=method.__name__):
                with self.assertRaisesRegex(TypeError, model_name):
                    method(value)
        self.assertEqual(opener.calls, [])

    def test_https_is_required_and_http_can_only_be_explicitly_enabled(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use https"):
            CenterAgentClient("http://localhost:8000", "token", "tenant", "agent", "channel")

        client = CenterAgentClient(
            "http://localhost:8000",
            "token",
            "tenant",
            "agent",
            "channel",
            opener=FakeOpener([]),
            allow_insecure_http=True,
        )
        self.assertEqual(client.base_url, "http://localhost:8000")

    def test_empty_token_and_unsafe_base_urls_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "agent_token"):
            CenterAgentClient("https://center.example.test", "  ", "tenant", "agent", "channel")
        with self.assertRaisesRegex(ValueError, "credentials"):
            CenterAgentClient(
                "https://user:pass@center.example.test", "token", "tenant", "agent", "channel"
            )
        with self.assertRaisesRegex(ValueError, "query or fragment"):
            CenterAgentClient(
                "https://center.example.test?token=x", "token", "tenant", "agent", "channel"
            )

    def test_protocol_identity_must_match_configured_agent_binding(self) -> None:
        client, opener = self.make_client([])

        with self.assertRaisesRegex(ValueError, "configured Agent binding"):
            client.report_inbound(replace(inbound(), tenant_id="tenant-2"))
        with self.assertRaisesRegex(ValueError, "configured Agent binding"):
            client.complete_delivery(replace(result(), agent_id="agent-2"))

        self.assertEqual(opener.calls, [])

    def test_401_and_500_are_clear_runtime_errors(self) -> None:
        for status in (401, 500):
            with self.subTest(status=status):
                client, _ = self.make_client([FakeResponse({"error": "denied"}, status=status)])
                with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                    client.report_inbound(inbound())

    def test_http_error_body_is_truncated_and_token_is_redacted(self) -> None:
        token = "agent-secret-token"
        body = (token + "-" + "x" * 1000).encode()
        error = HTTPError(
            "https://center.example.test/api/v1/agent/inbound",
            401,
            "Unauthorized",
            {},
            io.BytesIO(body),
        )
        client, _ = self.make_client([error])

        with self.assertRaises(RuntimeError) as raised:
            client.report_inbound(inbound())

        message = str(raised.exception)
        self.assertNotIn(token, message)
        self.assertIn("[REDACTED]", message)
        self.assertLessEqual(len(message), 515)

    def test_network_error_does_not_leak_token(self) -> None:
        client, _ = self.make_client([URLError("failed with agent-secret-token")])
        with self.assertRaises(RuntimeError) as raised:
            client.report_inbound(inbound())
        self.assertNotIn("agent-secret-token", str(raised.exception))

    def test_invalid_json_and_response_field_errors_are_runtime_errors(self) -> None:
        cases = [
            (FakeResponse(raw=b"not-json"), "invalid JSON"),
            (FakeResponse([]), "JSON object"),
            (FakeResponse({"accepted": "yes"}), "must be a boolean"),
        ]
        for response, message in cases:
            with self.subTest(message=message):
                client, _ = self.make_client([response])
                with self.assertRaisesRegex(RuntimeError, message):
                    client.report_inbound(inbound())

    def test_claim_rejects_bad_container_and_bad_delivery_fields(self) -> None:
        bad_command = {**command().to_dict(), "status": "invented"}
        cases = [
            (FakeResponse({"deliveries": {}}), "must be an array"),
            (FakeResponse({"deliveries": [bad_command]}), "invalid delivery command"),
        ]
        for response, message in cases:
            with self.subTest(message=message):
                client, _ = self.make_client([response])
                with self.assertRaisesRegex(RuntimeError, message):
                    client.claim_deliveries(1)


if __name__ == "__main__":
    unittest.main()
