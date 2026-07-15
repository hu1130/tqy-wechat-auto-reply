from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_ai.bot import WeChatAutoReplyBot
from wechat_ai.config import AppConfig


class FakeLLM:
    def __init__(self, reply: str = "reply", error: RuntimeError | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls = 0
        self.knowledge_contexts: list[str] = []

    def generate_reply(
        self,
        chat_name: str,
        history: list[dict[str, str]],
        knowledge_context: str = "",
    ) -> str:
        self.calls += 1
        self.knowledge_contexts.append(knowledge_context)
        if self.error:
            raise self.error
        return self.reply


class FakeMessage:
    def __init__(self, content: str = "hello", sender: str = "alice", msg_type: str = "text") -> None:
        self.content = content
        self.sender = sender
        self.type = msg_type


class FakeChat:
    who = "chat"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def SendMsg(self, message: str) -> None:
        self.sent.append(message)


def make_config(**overrides: object) -> AppConfig:
    config = AppConfig(
        llm_provider="ollama",
        llm_model="deepseek-r1:14b",
        ollama_base_url="http://localhost:11434",
        openai_base_url="https://api.openai.com/v1",
        openai_api_key="",
        request_timeout=120,
        wechat_listen_chats=["chat"],
        wechat_self_names=[],
        max_context_messages=8,
        dedup_window_seconds=60,
        dry_run=True,
        system_prompt="system",
        reply_allowed_chats=["chat"],
        reply_cooldown_seconds=0,
        default_chat_policy="auto",
        log_file="",
        audit_log_file="",
        state_db_file="",
        knowledge_file="",
    )
    return config.with_overrides(**overrides)


class BotSafetyTests(unittest.TestCase):
    def test_dry_run_generates_without_sending(self) -> None:
        chat = FakeChat()
        llm = FakeLLM()
        bot = WeChatAutoReplyBot(config=make_config(dry_run=True), llm_client=llm)

        bot._on_message(FakeMessage(), chat)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, [])

    def test_blocked_chat_skips_llm_and_send(self) -> None:
        chat = FakeChat()
        llm = FakeLLM()
        bot = WeChatAutoReplyBot(
            config=make_config(reply_blocked_chats=["chat"]),
            llm_client=llm,
        )

        bot._on_message(FakeMessage(), chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

    def test_non_text_message_type_skips_llm_and_send(self) -> None:
        chat = FakeChat()
        llm = FakeLLM()
        bot = WeChatAutoReplyBot(config=make_config(), llm_client=llm)

        bot._on_message(FakeMessage(msg_type="image"), chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

    def test_sensitive_input_skips_llm_and_send(self) -> None:
        chat = FakeChat()
        llm = FakeLLM()
        bot = WeChatAutoReplyBot(
            config=make_config(sensitive_keywords=["验证码"]),
            llm_client=llm,
        )

        bot._on_message(FakeMessage("验证码是多少"), chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

    def test_unsafe_reply_is_blocked_before_send(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="作为AI，我不能处理")
        bot = WeChatAutoReplyBot(
            config=make_config(
                dry_run=False,
                require_manual_confirm=False,
                blocked_reply_patterns=["作为AI"],
            ),
            llm_client=llm,
        )

        bot._on_message(FakeMessage(), chat)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, [])

    def test_chat_policy_dry_run_does_not_send_in_live_mode(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="policy dry-run")
        bot = WeChatAutoReplyBot(
            config=make_config(
                dry_run=False,
                chat_policies={"chat": "dry_run"},
            ),
            llm_client=llm,
        )

        bot._on_message(FakeMessage(), chat)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, [])

    def test_live_send_requires_manual_yes_when_enabled(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="approved")
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=True),
            llm_client=llm,
        )

        with patch("builtins.input", return_value="YES"):
            bot._on_message(FakeMessage(), chat)

        self.assertEqual(chat.sent, ["approved"])
        self.assertEqual(bot.sent_count, 1)

    def test_queue_approval_writes_pending_reply_without_sending(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="queued reply")
        with tempfile.TemporaryDirectory() as temp_dir:
            pending_path = Path(temp_dir) / "pending.jsonl"
            audit_path = Path(temp_dir) / "audit.jsonl"
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=True,
                    approval_mode="queue",
                    pending_replies_file=str(pending_path),
                    audit_log_file=str(audit_path),
                ),
                llm_client=llm,
            )

            bot._on_message(FakeMessage(), chat)

            pending_events = [json.loads(line) for line in pending_path.read_text(encoding="utf-8").splitlines()]
            audit_events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(chat.sent, [])
        self.assertEqual(bot.sent_count, 0)
        self.assertEqual(pending_events[-1]["action"], "pending_approval")
        self.assertEqual(pending_events[-1]["reply"], "queued reply")
        self.assertEqual(audit_events[-1]["action"], "pending_approval")

    def test_send_limit_skips_after_cap(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="limited")
        bot = WeChatAutoReplyBot(
            config=make_config(
                dry_run=False,
                require_manual_confirm=False,
                max_sends_per_run=1,
            ),
            llm_client=llm,
        )

        bot._on_message(FakeMessage("one"), chat)
        bot._on_message(FakeMessage("two"), chat)

        self.assertEqual(chat.sent, ["limited"])
        self.assertEqual(llm.calls, 1)

    def test_error_limit_pauses_bot(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(error=RuntimeError("LLM down"))
        bot = WeChatAutoReplyBot(
            config=make_config(max_errors_before_pause=1),
            llm_client=llm,
        )

        bot._on_message(FakeMessage(), chat)

        self.assertTrue(bot.paused)

    def test_error_pause_writes_alert_and_health_file(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(error=RuntimeError("LLM down"))
        with tempfile.TemporaryDirectory() as temp_dir:
            alert_path = Path(temp_dir) / "alerts.log"
            health_path = Path(temp_dir) / "health.json"
            bot = WeChatAutoReplyBot(
                config=make_config(
                    max_errors_before_pause=1,
                    alert_file=str(alert_path),
                    health_file=str(health_path),
                ),
                llm_client=llm,
            )

            bot._on_message(FakeMessage(), chat)

            alert_text = alert_path.read_text(encoding="utf-8")
            health_text = health_path.read_text(encoding="utf-8")

        self.assertIn("Bot paused after 1 consecutive errors", alert_text)
        self.assertIn('"status": "paused"', health_text)

    def test_audit_log_records_send_decision(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="audited")
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    audit_log_file=str(audit_path),
                ),
                llm_client=llm,
            )

            bot._on_message(FakeMessage(), chat)

            events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(events[-1]["action"], "send")
        self.assertEqual(events[-1]["chat_name"], "chat")
        self.assertTrue(events[-1]["sent"])

    def test_persisted_paused_chat_skips_llm(self) -> None:
        chat = FakeChat()
        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            bot = WeChatAutoReplyBot(
                config=make_config(state_db_file=str(db_path)),
                llm_client=llm,
            )
            bot.store.pause_chat("chat", reason="manual")

            bot._on_message(FakeMessage(), chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

    def test_persisted_policy_overrides_config_policy(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="persisted dry-run")
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(db_path),
                    default_chat_policy="auto",
                ),
                llm_client=llm,
            )
            bot.store.set_chat_policy("chat", "dry_run")

            bot._on_message(FakeMessage(), chat)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, [])

    def test_requires_knowledge_match_skips_unknown_question(self) -> None:
        chat = FakeChat()
        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_path = Path(temp_dir) / "faq.md"
            kb_path.write_text("# 上班时间\n工作日 9:00-18:00。", encoding="utf-8")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    knowledge_file=str(kb_path),
                    require_knowledge_match=True,
                ),
                llm_client=llm,
            )

            bot._on_message(FakeMessage("你们能退款吗"), chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

    def test_knowledge_context_is_passed_to_llm(self) -> None:
        chat = FakeChat()
        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_path = Path(temp_dir) / "faq.md"
            kb_path.write_text("# 发票\n发票问题需要人工确认。", encoding="utf-8")
            bot = WeChatAutoReplyBot(
                config=make_config(knowledge_file=str(kb_path)),
                llm_client=llm,
            )

            bot._on_message(FakeMessage("发票怎么开"), chat)

        self.assertEqual(llm.calls, 1)
        self.assertIn("发票问题需要人工确认", llm.knowledge_contexts[0])


if __name__ == "__main__":
    unittest.main()
