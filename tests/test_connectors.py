from __future__ import annotations

import unittest

from wechat_ai.config import AppConfig
from wechat_ai.connector_agent import build_connector, build_wechat_desktop_connector, connector_compatibility, list_connectors
from wechat_ai.connectors.registry import ConnectorDefinition, ConnectorRegistry
from wechat_ai.connectors.protocol import ConnectorCommand, OutboundMessage
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
    def __init__(self, content: str, sender: str = "alice", msg_type: str = "text", msg_id: str = "") -> None:
        self.content = content
        self.sender = sender
        self.type = msg_type
        self.id = msg_id
        self.attr = ""


class FakeWx:
    def __init__(self) -> None:
        self.current_chat = ""
        self.sent: list[tuple[str, str]] = []
        self.sessions = [FakeSession("KiraAI", "hello", isnew=True, new_count=1)]
        self.messages: dict[str, list[FakeMessage]] = {
            "KiraAI": [FakeMessage("old", msg_id="old-1")]
        }

    def IsOnline(self) -> bool:
        return True

    def GetMyInfo(self) -> dict[str, str]:
        return {"nickname": "operator"}

    def GetSession(self) -> list[FakeSession]:
        return self.sessions

    def ChatWith(self, who: str) -> None:
        self.current_chat = who

    def GetAllMessage(self) -> list[FakeMessage]:
        return self.messages.get(self.current_chat, [])

    def SendMsg(self, content: str) -> None:
        self.sent.append((self.current_chat, content))


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
        self.assertEqual(status.details["adapter"], "wxauto4_generic")
        self.assertTrue(status.details["capabilities"]["session_scan"])
        self.assertTrue(status.details["capabilities"]["polling_api"])
        self.assertEqual(wx.sent, [("KiraAI", "hello back")])

    def test_wechat_connector_poll_seeds_then_reads_changed_session(self) -> None:
        wx = FakeWx()
        connector = WeChatDesktopConnector(wx=wx, connector_id="pc-1", scan_all_sessions=True)
        connector.seed()
        wx.messages["KiraAI"].append(FakeMessage("new question", msg_id="new-1"))
        wx.sessions = [FakeSession("KiraAI", "new question", isnew=True, new_count=1)]

        messages = connector.poll()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].connector_id, "pc-1")
        self.assertEqual(messages[0].channel, "wechat_desktop")
        self.assertEqual(messages[0].conversation_id, "KiraAI")
        self.assertEqual(messages[0].content, "new question")

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
