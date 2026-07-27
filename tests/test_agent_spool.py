from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from wechat_ai.agent_spool import AgentSpool
from wechat_ai.center_agent import (
    AgentHeartbeat,
    DeliveryCommand,
    DeliveryResult,
    DeliveryStart,
    InboundEnvelope,
    OperationCommand,
    OperationResult,
    OperationStart,
    ContactSnapshot,
    FriendRequestSnapshot,
)
from wechat_ai.connector_agent import CenterConnectorAgent
from wechat_ai.connectors.protocol import ConnectorStatus, InboundMessage, OutboundMessage


def inbound() -> InboundEnvelope:
    return InboundEnvelope(
        "tenant-1",
        "agent-1",
        "channel-1",
        "message-1",
        "customer-1",
        "customer-1",
        "text",
        "hello",
        "2026-07-21T08:00:00+00:00",
    )


def command() -> DeliveryCommand:
    return DeliveryCommand(
        "tenant-1",
        "agent-1",
        "channel-1",
        "delivery-1",
        "lease-1",
        "customer-1",
        "reply",
        "claimed",
        1,
    )


def operation_command() -> OperationCommand:
    return OperationCommand(
        tenant_id="tenant-1",
        agent_id="agent-1",
        channel_account_id="channel-1",
        operation_id="operation-1",
        lease_token="operation-lease-1",
        operation_type="accept_friend",
        attempt=1,
        payload={"external_request_id": "wxfr_1"},
    )


class FakeConnector:
    connector_id = "connector-1"
    channel = "wechat_desktop"

    def __init__(self, *, send_error: Exception | None = None) -> None:
        self.send_error = send_error
        self.seeded = 0
        self.polls = 0
        self.sent: list[OutboundMessage] = []
        self.operations: list[tuple[str, dict[str, object]]] = []

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(
            self.connector_id,
            self.channel,
            True,
            "account",
            {"wechat_identity": {"account_id": "wxid-account", "nickname": "account"}},
        )

    def seed(self) -> None:
        self.seeded += 1

    def poll(self) -> list[InboundMessage]:
        self.polls += 1
        if self.polls > 1:
            return []
        return [
            InboundMessage(
                self.connector_id,
                self.channel,
                "customer-1",
                "customer-1",
                "text",
                "hello",
                "message-1",
                {"received_at": 1784620800},
            )
        ]

    def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)
        if self.send_error is not None:
            raise self.send_error

    def execute_operation(self, operation_type: str, payload: dict[str, object]) -> dict[str, object]:
        self.operations.append((operation_type, payload))
        return {"status": "succeeded", "evidence": {"verified": True}}


class SnapshotConnector(FakeConnector):
    def snapshot_contacts(self) -> list[dict[str, object]]:
        return [{"external_contact_id": "wxc_1", "display_name": "Alice", "remark": "", "kind": "friend", "status": "active", "raw": {"source": "sessions"}}]

    def snapshot_friend_requests(self) -> list[dict[str, object]]:
        return [{"external_request_id": "wxfr_1", "display_name": "Bob", "verification_message": "hello", "status": "pending", "raw": {}}]


class FakeClient:
    tenant_id = "tenant-1"
    agent_id = "agent-1"
    channel_account_id = "channel-1"

    def __init__(
        self,
        *,
        complete_error: RuntimeError | None = None,
        send_allowed: bool = True,
    ) -> None:
        self.complete_error = complete_error
        self.send_allowed = send_allowed
        self.heartbeats: list[AgentHeartbeat] = []
        self.inbound: list[InboundEnvelope] = []
        self.starts: list[DeliveryStart] = []
        self.results: list[DeliveryResult] = []
        self.operation_starts: list[OperationStart] = []
        self.operation_results: list[OperationResult] = []
        self.contact_snapshots: list[ContactSnapshot] = []
        self.friend_request_snapshots: list[FriendRequestSnapshot] = []
        self.claimed = False
        self.operation_claimed = True

    def heartbeat(self, value: AgentHeartbeat) -> dict[str, object]:
        self.heartbeats.append(value)
        return {
            "accepted": True,
            "identity_status": "verified",
            "operations_allowed": True,
            "wechat_identity": dict(value.wechat_identity),
        }

    def report_inbound(self, value: InboundEnvelope) -> None:
        self.inbound.append(value)

    def claim_deliveries(self, limit: int) -> list[DeliveryCommand]:
        if self.claimed:
            return []
        self.claimed = True
        return [command()]

    def start_delivery(self, value: DeliveryStart) -> bool:
        self.starts.append(value)
        return self.send_allowed

    def complete_delivery(self, value: DeliveryResult) -> None:
        self.results.append(value)
        if self.complete_error is not None:
            raise self.complete_error

    def claim_operations(self, limit: int) -> list[OperationCommand]:
        if self.operation_claimed:
            return []
        self.operation_claimed = True
        return [operation_command()]

    def start_operation(self, value: OperationStart) -> None:
        self.operation_starts.append(value)

    def complete_operation(self, value: OperationResult) -> None:
        self.operation_results.append(value)
        if self.complete_error is not None:
            raise self.complete_error

    def sync_contacts(self, value: ContactSnapshot) -> None:
        self.contact_snapshots.append(value)

    def sync_friend_requests(self, value: FriendRequestSnapshot) -> None:
        self.friend_request_snapshots.append(value)


class AgentSpoolTests(unittest.TestCase):
    def test_operation_result_survives_reopen_and_executing_recovers_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spool.sqlite3"
            spool = AgentSpool(path)
            spool.enqueue_operation(operation_command())
            spool.mark_operation_starting("operation-1", "operation-lease-1")
            spool.mark_operation_executing("operation-1", "operation-lease-1")
            spool.close()

            reopened = AgentSpool(path)
            self.assertEqual(reopened.recover_interrupted_operations(), 1)
            pending = reopened.pending_operations()
            self.assertEqual(pending[0].status, "uncertain")
            self.assertIn("actual WeChat result is unknown", pending[0].result_error)
            reopened.mark_operation_reported("operation-1", "operation-lease-1")
            self.assertEqual(reopened.pending_operations(), [])
            reopened.close()

    def test_older_operation_attempt_cannot_replace_newer_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = AgentSpool(Path(directory) / "spool.sqlite3")
            newer = OperationCommand(
                **{
                    **operation_command().to_dict(),
                    "attempt": 2,
                    "lease_token": "operation-lease-2",
                }
            )
            spool.enqueue_operation(newer)
            spool.mark_operation_starting("operation-1", "operation-lease-2")
            spool.mark_operation_executing("operation-1", "operation-lease-2")
            spool.mark_operation_result(
                "operation-1",
                "operation-lease-2",
                "succeeded",
                evidence={"verified": True},
            )
            spool.enqueue_operation(operation_command())

            pending = spool.pending_operations()
            self.assertEqual(pending[0].command.attempt, 2)
            self.assertEqual(pending[0].status, "succeeded")
            self.assertEqual(pending[0].result_evidence, {"verified": True})
            spool.close()

    def test_crash_after_center_start_retries_start_without_reexecuting_wechat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spool.sqlite3"
            connector = FakeConnector()
            client = FakeClient()
            client.claimed = True
            client.operation_claimed = False
            spool = AgentSpool(path)
            with patch.object(
                spool,
                "mark_operation_executing",
                side_effect=RuntimeError("simulated process crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated process crash"):
                    CenterConnectorAgent(connector, client, spool).run_once()
            self.assertEqual(connector.operations, [])
            self.assertEqual(spool.pending_operations()[0].status, "claimed")
            spool.close()

            reopened = AgentSpool(path)
            CenterConnectorAgent(connector, client, reopened).run_once()

            self.assertEqual(len(client.operation_starts), 2)
            self.assertEqual(len(connector.operations), 1)
            self.assertEqual(reopened.pending_operations(), [])
            reopened.close()

    def test_inbound_and_interrupted_send_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spool.sqlite3"
            spool = AgentSpool(path)
            spool.enqueue_inbound(inbound())
            spool.enqueue_delivery(command())
            spool.mark_delivery_starting("delivery-1", "lease-1")
            spool.mark_delivery_sending("delivery-1", "lease-1")
            spool.close()

            reopened = AgentSpool(path)
            self.assertEqual(reopened.pending_inbound(), [inbound()])
            self.assertEqual(reopened.recover_interrupted_sends(), 1)
            pending = reopened.pending_deliveries()
            self.assertEqual(pending[0].status, "uncertain")
            self.assertIn("actual WeChat result is unknown", pending[0].result_error)
            reopened.close()

    def test_crash_after_delivery_start_retries_start_without_resending_wechat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spool.sqlite3"
            connector = FakeConnector()
            client = FakeClient()
            spool = AgentSpool(path)
            with patch.object(
                spool,
                "mark_delivery_sending",
                side_effect=RuntimeError("simulated process crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated process crash"):
                    CenterConnectorAgent(connector, client, spool).run_once()
            self.assertEqual(connector.sent, [])
            self.assertEqual(spool.pending_deliveries()[0].status, "claimed")
            spool.close()

            reopened = AgentSpool(path)
            CenterConnectorAgent(connector, client, reopened).run_once()

            self.assertEqual(len(client.starts), 2)
            self.assertEqual(len(connector.sent), 1)
            self.assertEqual(reopened.pending_deliveries(), [])
            reopened.close()

    def test_runner_persists_reports_and_completes_delivery_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = AgentSpool(Path(directory) / "spool.sqlite3")
            connector = FakeConnector()
            client = FakeClient()
            agent = CenterConnectorAgent(connector, client, spool)

            stats = agent.run_once()
            agent.run_once()

            self.assertEqual(connector.seeded, 1)
            self.assertEqual(len(client.inbound), 1)
            self.assertEqual(len(client.starts), 1)
            self.assertEqual(len(connector.sent), 1)
            self.assertEqual([item.status for item in client.results], ["sent"])
            self.assertEqual(
                stats,
                {
                    "captured": 1,
                    "reported": 1,
                    "claimed": 1,
                    "completed": 1,
                    "contacts_synced": 0,
                    "friend_requests_synced": 0,
                    "operations_claimed": 0,
                    "operations_completed": 0,
                    "identity_blocked": 0,
                },
            )
            self.assertEqual(spool.pending_inbound(), [])
            self.assertEqual(spool.pending_deliveries(), [])
            spool.close()

    def test_runner_discards_delivery_suppressed_by_center_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = AgentSpool(Path(directory) / "spool.sqlite3")
            connector = FakeConnector()
            client = FakeClient(send_allowed=False)

            CenterConnectorAgent(connector, client, spool).run_once()

            self.assertEqual(len(client.starts), 1)
            self.assertEqual(connector.sent, [])
            self.assertEqual(spool.pending_deliveries(), [])
            spool.close()

    def test_send_exception_is_uncertain_and_never_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spool.sqlite3"
            spool = AgentSpool(path)
            connector = FakeConnector(send_error=RuntimeError("window outcome unknown"))
            offline_client = FakeClient(complete_error=RuntimeError("center offline"))
            agent = CenterConnectorAgent(connector, offline_client, spool)
            agent.run_once()
            self.assertEqual(len(connector.sent), 1)
            self.assertEqual(spool.pending_deliveries()[0].status, "uncertain")
            spool.close()

            reopened = AgentSpool(path)
            healthy_client = FakeClient()
            healthy_client.claimed = True
            restarted = CenterConnectorAgent(connector, healthy_client, reopened)
            restarted.run_once()

            self.assertEqual(len(connector.sent), 1)
            self.assertEqual([item.status for item in healthy_client.results], ["uncertain"])
            self.assertEqual(reopened.pending_deliveries(), [])
            reopened.close()

    def test_runner_executes_claimed_operation_once_and_reports_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = AgentSpool(Path(directory) / "spool.sqlite3")
            connector = FakeConnector()
            client = FakeClient()
            client.claimed = True
            client.operation_claimed = False
            agent = CenterConnectorAgent(connector, client, spool)

            first = agent.run_once()
            second = agent.run_once()

            self.assertEqual(first["operations_claimed"], 1)
            self.assertEqual(first["operations_completed"], 1)
            self.assertEqual(second["operations_completed"], 0)
            self.assertEqual(connector.operations, [("accept_friend", {"external_request_id": "wxfr_1"})])
            self.assertEqual(len(client.operation_starts), 1)
            self.assertEqual([item.status for item in client.operation_results], ["succeeded"])
            self.assertEqual(spool.pending_operations(), [])
            spool.close()

    def test_runner_syncs_typed_contact_and_friend_request_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = AgentSpool(Path(directory) / "spool.sqlite3")
            connector = SnapshotConnector()
            client = FakeClient()
            client.claimed = True
            agent = CenterConnectorAgent(connector, client, spool)

            stats = agent.run_once()

            self.assertEqual(stats["contacts_synced"], 1)
            self.assertEqual(stats["friend_requests_synced"], 1)
            self.assertEqual(client.contact_snapshots[0].contacts[0].external_contact_id, "wxc_1")
            self.assertEqual(client.friend_request_snapshots[0].requests[0].external_request_id, "wxfr_1")
            self.assertTrue(client.friend_request_snapshots[0].requests[0].requested_at)
            spool.close()

    def test_runner_sends_reply_before_ui_maintenance_snapshots(self) -> None:
        class ReplyPriorityConnector(SnapshotConnector):
            sent_before_contact_sync = -1
            sent_before_friend_request_sync = -1

            def snapshot_contacts(self) -> list[dict[str, object]]:
                self.sent_before_contact_sync = len(self.sent)
                return super().snapshot_contacts()

            def snapshot_friend_requests(self) -> list[dict[str, object]]:
                self.sent_before_friend_request_sync = len(self.sent)
                return super().snapshot_friend_requests()

        with tempfile.TemporaryDirectory() as directory:
            spool = AgentSpool(Path(directory) / "spool.sqlite3")
            connector = ReplyPriorityConnector()
            client = FakeClient()
            agent = CenterConnectorAgent(connector, client, spool)

            agent.run_once()

            self.assertEqual(connector.sent_before_contact_sync, 1)
            self.assertEqual(connector.sent_before_friend_request_sync, 1)
            spool.close()

    def test_operation_result_is_replayed_after_center_outage_without_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spool.sqlite3"
            connector = FakeConnector()
            offline_client = FakeClient(complete_error=RuntimeError("center offline"))
            offline_client.claimed = True
            offline_client.operation_claimed = False
            spool = AgentSpool(path)
            CenterConnectorAgent(connector, offline_client, spool).run_once()
            self.assertEqual(len(connector.operations), 1)
            self.assertEqual(spool.pending_operations()[0].status, "succeeded")
            spool.close()

            healthy_client = FakeClient()
            healthy_client.claimed = True
            reopened = AgentSpool(path)
            CenterConnectorAgent(connector, healthy_client, reopened).run_once()

            self.assertEqual(len(connector.operations), 1)
            self.assertEqual([item.status for item in healthy_client.operation_results], ["succeeded"])
            self.assertEqual(reopened.pending_operations(), [])
            reopened.close()


if __name__ == "__main__":
    unittest.main()
