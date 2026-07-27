from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_ai.config import AppConfig
from wechat_ai.connector_agent import (
    CenterConnectorAgent,
    _write_center_agent_health,
    build_connector,
    build_wechat_desktop_connector,
    connector_compatibility,
    list_connectors,
    packaged_updater_status,
)
from wechat_ai.agent_updater import UpdateStateStore
from wechat_ai.connectors.registry import ConnectorDefinition, ConnectorRegistry
from wechat_ai.connectors.protocol import ConnectorCommand, OutboundMessage
from wechat_ai.connectors.protocol import ConnectorStatus
from wechat_ai.connectors.wechat_desktop import WeChatDesktopConnector


class FakeSession:
    def __init__(self, name: str, content: str, isnew: bool = False, new_count: int = 0) -> None:
        self.name = name
        self.time = "2026-07-16 12:00:00"
        self.content = content
        self.isnew = isnew
        self.new_count = new_count
        self.info = {
            "name": name,
            "time": self.time,
            "content": content,
            "isnew": isnew,
            "new_count": new_count,
        }


class FakeMessage:
    def __init__(
        self,
        content: str,
        sender: str = "alice",
        msg_type: str = "text",
        msg_id: str = "",
        attr: str = "",
    ) -> None:
        self.content = content
        self.sender = sender
        self.type = msg_type
        self.id = msg_id
        self.attr = attr


class FakeWx:
    client_version = "4.1.8.107"
    def __init__(self) -> None:
        self.current_chat = ""
        self.current_view = "chat"
        self.sent: list[tuple[str, str]] = []
        self.sessions = [FakeSession("KiraAI", "hello", isnew=True, new_count=1)]
        self.messages: dict[str, list[FakeMessage]] = {
            "KiraAI": [FakeMessage("old", msg_id="old-1")]
        }

    def IsOnline(self) -> bool:
        return True

    def GetMyInfo(self) -> dict[str, str]:
        return {"wxid": "wxid_operator", "nickname": "operator"}

    def GetSession(self) -> list[FakeSession]:
        return self.sessions

    def ChatWith(self, who: str) -> None:
        self.current_chat = who

    def GetAllMessage(self) -> list[FakeMessage]:
        return self.messages.get(self.current_chat, [])

    def SendMsg(self, content: str) -> None:
        self.sent.append((self.current_chat, content))

    def SwitchToContact(self) -> None:
        self.current_view = "contacts"

    def SwitchToChat(self) -> None:
        self.current_view = "chat"


class FakeNoContactsWx(FakeWx):
    SwitchToContact = None


class FakeUnknownVersionWx(FakeWx):
    client_version = ""


class FakeBaselineFailureWx(FakeWx):
    def GetAllMessage(self) -> list[FakeMessage]:
        raise RuntimeError("message pane unavailable")


class FakeIdentityShapesWx(FakeWx):
    def GetMyInfo(self) -> dict[str, str]:
        return {
            "WeChat-ID": "wxid_primary",
            "ID": "fallback-id",
            "display name": "Operator Name",
            "phone": "13800000000",
            "avatar": "C:/private/avatar.png",
        }


class GateConnector:
    connector_id = "connector-1"
    channel = "wechat_desktop"

    def __init__(self, identity: dict[str, str]) -> None:
        self.identity = dict(identity)
        self.seeded = 0
        self.polled = 0
        self.contacts_snapshotted = 0
        self.friend_requests_snapshotted = 0
        self.events: list[str] = []

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(
            self.connector_id,
            self.channel,
            True,
            self.identity.get("nickname", ""),
            {
                "wechat_identity": dict(self.identity),
                "capabilities": {"send_text": True, "session_scan": True},
            },
        )

    def seed(self) -> None:
        self.seeded += 1
        self.events.append("seed")

    def poll(self) -> list[object]:
        self.polled += 1
        self.events.append("poll")
        return []

    def snapshot_contacts(self) -> list[dict[str, object]]:
        self.contacts_snapshotted += 1
        return []

    def snapshot_friend_requests(self) -> list[dict[str, object]]:
        self.friend_requests_snapshotted += 1
        return []


class GateSpool:
    def __init__(self) -> None:
        self.pending_checks = 0

    def recover_interrupted_sends(self) -> int:
        return 0

    def recover_interrupted_operations(self) -> int:
        return 0

    def cleanup_reported(self) -> int:
        return 0

    def pending_inbound(self) -> list[object]:
        self.pending_checks += 1
        return []

    def pending_deliveries(self) -> list[object]:
        self.pending_checks += 1
        return []

    def pending_operations(self) -> list[object]:
        self.pending_checks += 1
        return []


class GateClient:
    tenant_id = "tenant-1"
    agent_id = "agent-1"
    channel_account_id = "channel-1"

    def __init__(self, heartbeat_responses: list[object]) -> None:
        self.heartbeat_responses = list(heartbeat_responses)
        self.heartbeats: list[object] = []
        self.claimed_deliveries = 0
        self.claimed_operations = 0
        self.contacts_synced = 0
        self.friend_requests_synced = 0

    def heartbeat(self, value: object) -> object:
        self.heartbeats.append(value)
        response = (
            self.heartbeat_responses.pop(0)
            if len(self.heartbeat_responses) > 1
            else self.heartbeat_responses[0]
        )
        if isinstance(response, BaseException):
            raise response
        return response

    def claim_deliveries(self, limit: int) -> list[object]:
        self.claimed_deliveries += 1
        return []

    def claim_operations(self, limit: int) -> list[object]:
        self.claimed_operations += 1
        return []

    def sync_contacts(self, value: object) -> None:
        self.contacts_synced += 1

    def sync_friend_requests(self, value: object) -> None:
        self.friend_requests_synced += 1


def gate_response(
    identity_status: str,
    operations_allowed: bool,
    identity: dict[str, str],
) -> dict[str, object]:
    return {
        "accepted": True,
        "identity_status": identity_status,
        "operations_allowed": operations_allowed,
        "wechat_identity": dict(identity),
    }


class CenterAgentHealthTests(unittest.TestCase):
    def test_writes_center_agent_identity_and_loop_stats(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "health.json"
            config = replace(
                make_config([]),
                health_file=str(path),
                agent_tenant_id="tenant-a",
                agent_instance_id="agent-a",
                agent_channel_account_id="channel-a",
            )

            _write_center_agent_health(
                config,
                "running",
                "loop completed",
                stats={"captured": 1},
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["component"], "center_agent")
            self.assertEqual(payload["tenant_id"], "tenant-a")
            self.assertEqual(payload["stats"], {"captured": 1})


def make_config(chats: list[str]) -> AppConfig:
    return AppConfig(
        llm_provider="ollama",
        llm_model="deepseek-r1:14b",
        ollama_base_url="http://localhost:11434",
        openai_base_url="https://api.openai.com/v1",
        openai_api_key="",
        request_timeout=120,
        wechat_listen_chats=chats,
        wechat_self_names=[],
        max_context_messages=8,
        message_buffer_seconds=3,
        dedup_window_seconds=60,
        dry_run=True,
        system_prompt="system",
        reply_allowed_chats=["*"],
        log_file="",
        audit_log_file="",
        state_db_file="",
        knowledge_file="",
    )


class ConnectorProtocolTests(unittest.TestCase):
    def test_wechat_connector_status_and_send(self) -> None:
        wx = FakeWx()
        connector = WeChatDesktopConnector(wx=wx, connector_id="pc-1", scan_all_sessions=True)

        status = connector.status()
        connector.send(
            OutboundMessage(
                connector_id="pc-1",
                channel="wechat_desktop",
                conversation_id="KiraAI",
                content="hello back",
            )
        )

        self.assertTrue(status.online)
        self.assertEqual(status.account_name, "operator")
        self.assertEqual(
            status.details["wechat_identity"],
            {"account_id": "wxid_operator", "nickname": "operator"},
        )
        self.assertEqual(status.details["adapter"], "wxauto4_generic")
        self.assertTrue(status.details["capabilities"]["session_scan"])
        self.assertTrue(status.details["capabilities"]["polling_api"])
        self.assertEqual(wx.sent, [("KiraAI", "hello back")])

    def test_wechat_identity_uses_only_normalized_allowlisted_fields(self) -> None:
        connector = WeChatDesktopConnector(
            wx=FakeIdentityShapesWx(),
            connector_id="pc-1",
        )

        status = connector.status()

        self.assertEqual(status.account_name, "Operator Name")
        self.assertEqual(
            status.details["wechat_identity"],
            {"account_id": "wxid_primary", "nickname": "Operator Name"},
        )
        self.assertNotIn("phone", status.details)
        self.assertNotIn("avatar", status.details)

    def test_center_agent_rechecks_pending_identity_each_loop_without_operations(self) -> None:
        identity = {"account_id": "wxid_operator", "nickname": "operator"}
        connector = GateConnector(identity)
        spool = GateSpool()
        client = GateClient([gate_response("pending", False, identity)])
        agent = CenterConnectorAgent(connector, client, spool)

        first = agent.run_once()
        second = agent.run_once()

        self.assertEqual(len(client.heartbeats), 2)
        self.assertEqual(client.heartbeats[0].wechat_identity, identity)
        self.assertEqual(
            client.heartbeats[0].capabilities["connector"]["send_text"],
            True,
        )
        self.assertEqual(first["identity_blocked"], 1)
        self.assertEqual(second["identity_blocked"], 1)
        self.assertEqual(connector.seeded, 0)
        self.assertEqual(connector.polled, 0)
        self.assertEqual(connector.contacts_snapshotted, 0)
        self.assertEqual(connector.friend_requests_snapshotted, 0)
        self.assertEqual(spool.pending_checks, 0)
        self.assertEqual(client.claimed_deliveries, 0)
        self.assertEqual(client.claimed_operations, 0)

    def test_center_agent_reports_sanitized_packaged_updater_state(self) -> None:
        identity = {"account_id": "wxid_operator", "nickname": "operator"}
        connector = GateConnector(identity)
        client = GateClient([gate_response("pending", False, identity)])
        with tempfile.TemporaryDirectory() as raw_dir:
            UpdateStateStore(raw_dir).initialize("1.2.3")
            with patch.dict(
                "os.environ",
                {
                    "WECHAT_AGENT_INSTALL_ROOT": raw_dir,
                    "WECHAT_AGENT_RUNNING_VERSION": "1.2.3",
                },
                clear=False,
            ):
                agent = CenterConnectorAgent(connector, client, GateSpool())
                agent.run_once()

        updater = client.heartbeats[0].capabilities["updater"]
        self.assertEqual(updater["status"], "healthy")
        self.assertEqual(updater["active_version"], "1.2.3")
        self.assertEqual(updater["running_version"], "1.2.3")
        self.assertNotIn("failure_summary", updater)

    def test_packaged_updater_status_fails_closed_without_path_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            with patch.dict(
                "os.environ",
                {
                    "WECHAT_AGENT_INSTALL_ROOT": raw_dir,
                    "WECHAT_AGENT_RUNNING_VERSION": "1.2.3",
                },
                clear=False,
            ):
                status = packaged_updater_status()

        self.assertEqual(status["status"], "state_unavailable")
        self.assertEqual(status["failure_code"], "update_state_unavailable")
        self.assertNotIn(raw_dir, repr(status))

    def test_center_agent_seeds_before_operations_when_identity_becomes_verified(self) -> None:
        identity = {"account_id": "wxid_operator", "nickname": "operator"}
        connector = GateConnector(identity)
        client = GateClient(
            [
                gate_response("pending", False, identity),
                gate_response("verified", True, identity),
            ]
        )
        agent = CenterConnectorAgent(connector, client, GateSpool())

        first = agent.run_once()
        second = agent.run_once()

        self.assertEqual(first["identity_blocked"], 1)
        self.assertEqual(second["identity_blocked"], 0)
        self.assertEqual(connector.events[:2], ["seed", "poll"])
        self.assertEqual(connector.seeded, 1)
        self.assertEqual(client.claimed_deliveries, 1)
        self.assertEqual(client.claimed_operations, 1)

    def test_center_agent_heartbeat_failure_immediately_closes_the_gate(self) -> None:
        identity = {"account_id": "wxid_operator", "nickname": "operator"}
        connector = GateConnector(identity)
        client = GateClient(
            [gate_response("verified", True, identity), RuntimeError("center offline")]
        )
        agent = CenterConnectorAgent(
            connector,
            client,
            GateSpool(),
            heartbeat_interval_seconds=0,
        )

        first = agent.run_once()
        second = agent.run_once()

        self.assertEqual(first["identity_blocked"], 0)
        self.assertEqual(second["identity_blocked"], 1)
        self.assertEqual(connector.polled, 1)
        self.assertEqual(client.claimed_deliveries, 1)

    def test_center_agent_detects_local_account_change_before_next_interval(self) -> None:
        original = {"account_id": "wxid_operator", "nickname": "operator"}
        changed = {"account_id": "wxid_other", "nickname": "other"}
        connector = GateConnector(original)
        client = GateClient(
            [
                gate_response("verified", True, original),
                gate_response("mismatch", False, changed),
            ]
        )
        agent = CenterConnectorAgent(
            connector,
            client,
            GateSpool(),
            heartbeat_interval_seconds=3600,
        )

        agent.run_once()
        connector.identity = changed
        blocked = agent.run_once()

        self.assertEqual(len(client.heartbeats), 2)
        self.assertEqual(client.heartbeats[-1].wechat_identity, changed)
        self.assertEqual(blocked["identity_blocked"], 1)
        self.assertEqual(connector.seeded, 1)
        self.assertEqual(connector.polled, 1)

    def test_center_agent_allows_nickname_change_for_same_account_id(self) -> None:
        original = {"account_id": "wxid_operator", "nickname": "old name"}
        renamed = {"account_id": "wxid_operator", "nickname": "new name"}
        connector = GateConnector(original)
        client = GateClient([gate_response("verified", True, original)])
        agent = CenterConnectorAgent(
            connector,
            client,
            GateSpool(),
            heartbeat_interval_seconds=3600,
        )

        first = agent.run_once()
        connector.identity = renamed
        second = agent.run_once()

        self.assertEqual(first["identity_blocked"], 0)
        self.assertEqual(second["identity_blocked"], 0)
        self.assertEqual(len(client.heartbeats), 1)
        self.assertEqual(connector.seeded, 1)
        self.assertEqual(connector.polled, 2)

    def test_contact_snapshot_marks_display_name_identity_fallback(self) -> None:
        wx = FakeWx()
        connector = WeChatDesktopConnector(wx=wx, connector_id="pc-1")

        def scan_contacts(*, wechat: FakeWx) -> list[dict[str, object]]:
            wechat.SwitchToContact()
            return [
                {
                    "external_contact_id": "wxc_ui",
                    "display_name": "KiraAI",
                    "remark": "",
                    "kind": "friend",
                    "status": "active",
                    "raw": {"source": "wechat_contacts_ui", "identity_quality": "automation_id"},
                }
            ]

        with patch(
            "wechat_ai.connectors.wechat_adapters.wxauto4_adapter.scan_channel_contacts",
            side_effect=scan_contacts,
        ):
            contacts = connector.snapshot_contacts()

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["display_name"], "KiraAI")
        self.assertEqual(contacts[0]["kind"], "friend")
        self.assertEqual(contacts[0]["raw"]["source"], "wechat_contacts_ui+wxauto4_sessions")
        self.assertEqual(contacts[0]["raw"]["identity_quality"], "automation_id")
        self.assertEqual(wx.current_view, "chat")

    def test_friend_request_snapshot_restores_chat_view(self) -> None:
        wx = FakeWx()
        connector = WeChatDesktopConnector(wx=wx, connector_id="pc-1")

        def scan_requests(*, wechat: FakeWx) -> list[object]:
            wechat.SwitchToContact()
            return []

        with patch(
            "wechat_ai.connectors.wechat_adapters.wxauto4_adapter.scan_friend_requests",
            side_effect=scan_requests,
        ):
            requests = connector.snapshot_friend_requests()

        self.assertEqual(requests, [])
        self.assertEqual(wx.current_view, "chat")

    def test_friend_operation_fails_before_ui_when_adapter_capability_is_missing(self) -> None:
        wx = FakeNoContactsWx()
        connector = WeChatDesktopConnector(wx=wx, connector_id="pc-1")

        with self.assertRaisesRegex(RuntimeError, "friend_request_accept"):
            connector.execute_operation("accept_friend", {"external_request_id": "wxfr_1"})

        self.assertEqual(wx.sent, [])

    def test_unknown_wechat_version_is_offline_to_agent_and_blocks_ui(self) -> None:
        wx = FakeUnknownVersionWx()
        connector = WeChatDesktopConnector(wx=wx, connector_id="pc-1")

        status = connector.status()

        self.assertFalse(status.online)
        self.assertFalse(status.details["compatible"])
        with self.assertRaisesRegex(RuntimeError, "not in the verified"):
            connector.send(
                OutboundMessage(
                    connector_id="pc-1",
                    channel="wechat_desktop",
                    conversation_id="KiraAI",
                    content="must not send",
                )
            )
        self.assertEqual(wx.sent, [])

    def test_wechat_connector_poll_seeds_then_reads_changed_session(self) -> None:
        wx = FakeWx()
        connector = WeChatDesktopConnector(wx=wx, connector_id="pc-1", scan_all_sessions=True)
        connector.seed()
        wx.messages["KiraAI"].append(
            FakeMessage("agent reply", sender="operator", msg_id="self-1", attr="self")
        )
        wx.messages["KiraAI"].append(
            FakeMessage("system notice", sender="system", msg_id="system-1", attr="system")
        )
        wx.messages["KiraAI"].append(FakeMessage("new question", msg_id="new-1"))
        wx.sessions = [FakeSession("KiraAI", "new question", isnew=True, new_count=1)]

        messages = connector.poll()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].connector_id, "pc-1")
        self.assertEqual(messages[0].channel, "wechat_desktop")
        self.assertEqual(messages[0].conversation_id, "KiraAI")
        self.assertEqual(messages[0].content, "new question")

    def test_wechat_connector_stops_when_startup_baseline_cannot_be_read(self) -> None:
        connector = WeChatDesktopConnector(
            wx=FakeBaselineFailureWx(),
            connector_id="pc-1",
            scan_all_sessions=True,
        )

        with self.assertRaisesRegex(RuntimeError, "connector startup baseline"):
            connector.seed()

        self.assertNotIn("KiraAI", connector.seen)

    def test_wechat_connector_command_send_message(self) -> None:
        wx = FakeWx()
        connector = WeChatDesktopConnector(wx=wx, connector_id="pc-1", scan_all_sessions=True)

        event = connector.handle_command(
            ConnectorCommand(
                command="send_message",
                connector_id="pc-1",
                channel="wechat_desktop",
                conversation_id="KiraAI",
                content="from command",
            )
        )

        self.assertEqual(event.event, "message_sent")
        self.assertEqual(wx.sent, [("KiraAI", "from command")])

    def test_agent_builds_scan_connector_for_star_chat(self) -> None:
        connector = build_wechat_desktop_connector(make_config(["*"]), connector_id="pc-1")

        self.assertEqual(connector.connector_id, "pc-1")
        self.assertTrue(connector.scan_all_sessions)
        self.assertEqual(connector.listen_chats, [])
        self.assertEqual(
            connector.operation_timeout_seconds,
            make_config(["*"]).wechat_ui_operation_timeout_seconds,
        )

    def test_agent_builds_named_chat_connector(self) -> None:
        connector = build_wechat_desktop_connector(make_config(["KiraAI"]), connector_id="pc-1")

        self.assertFalse(connector.scan_all_sessions)
        self.assertEqual(connector.listen_chats, ["KiraAI"])

    def test_registry_lists_default_wechat_desktop_connector(self) -> None:
        connectors = list_connectors()

        self.assertEqual(connectors[0]["channel"], "wechat_desktop")
        self.assertIn("wxauto4", connectors[0]["description"])

    def test_build_connector_uses_registered_channel(self) -> None:
        connector = build_connector(make_config(["KiraAI"]), channel="wechat_desktop", connector_id="pc-1")

        self.assertEqual(connector.connector_id, "pc-1")
        self.assertEqual(connector.channel, "wechat_desktop")
        self.assertEqual(connector.listen_chats, ["KiraAI"])

    def test_registry_rejects_unknown_channel(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Unsupported connector channel"):
            build_connector(make_config(["KiraAI"]), channel="missing", connector_id="pc-1")

    def test_registry_rejects_duplicate_channel(self) -> None:
        registry = ConnectorRegistry()
        definition = ConnectorDefinition(
            channel="wechat_desktop",
            description="one",
            builder=build_wechat_desktop_connector,
        )
        registry.register(definition)

        with self.assertRaisesRegex(RuntimeError, "already registered"):
            registry.register(definition)

    def test_connector_compatibility_report_uses_adapter_capabilities(self) -> None:
        class ConnectorWithFakeWx(WeChatDesktopConnector):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, wx=FakeWx(), **kwargs)

        original = __import__("wechat_ai.connector_agent", fromlist=["WeChatDesktopConnector"])
        old_class = original.WeChatDesktopConnector
        original.WeChatDesktopConnector = ConnectorWithFakeWx
        try:
            report = connector_compatibility(make_config(["*"]), connector_id="pc-1")
        finally:
            original.WeChatDesktopConnector = old_class

        self.assertEqual(report["connector_id"], "pc-1")
        self.assertEqual(report["adapter"], "wxauto4_generic")
        self.assertTrue(report["capabilities"]["session_scan"])
        self.assertEqual(report["missing"], [])


if __name__ == "__main__":
    unittest.main()
