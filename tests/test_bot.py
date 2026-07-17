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
        self.histories: list[list[dict[str, str]]] = []

    def generate_reply(
        self,
        chat_name: str,
        history: list[dict[str, str]],
        knowledge_context: str = "",
    ) -> str:
        self.calls += 1
        self.knowledge_contexts.append(knowledge_context)
        self.histories.append(history)
        if self.error:
            raise self.error
        return self.reply


class FakeMessage:
    def __init__(self, content: str = "hello", sender: str = "alice", msg_type: str = "text") -> None:
        self.content = content
        self.sender = sender
        self.type = msg_type


class FakeVoiceMessage(FakeMessage):
    def __init__(self, text: str = "voice text", sender: str = "alice", content: str = "", msg_type: str = "voice") -> None:
        super().__init__(content=content, sender=sender, msg_type=msg_type)
        self.text = text
        self.to_text_calls = 0

    def to_text(self) -> str:
        self.to_text_calls += 1
        return self.text


class FakeBrokenVoiceMessage(FakeVoiceMessage):
    def to_text(self) -> str:
        self.to_text_calls += 1
        raise RuntimeError("voice engine unavailable")


class FakeFlakyVoiceMessage(FakeVoiceMessage):
    def to_text(self) -> str:
        self.to_text_calls += 1
        if self.to_text_calls <= 2:
            return ""
        return self.text


class FakeChat:
    who = "chat"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def SendMsg(self, message: str) -> None:
        self.sent.append(message)


class FakeWx:
    def __init__(self, messages: list[FakeMessage] | None = None) -> None:
        self.current_chat = ""
        self.sent: list[tuple[str, str]] = []
        self.messages = messages or []

    def ChatWith(self, chat_name: str) -> None:
        self.current_chat = chat_name

    def GetAllMessage(self) -> list[FakeMessage]:
        return self.messages

    def SendMsg(self, message: str) -> None:
        self.sent.append((self.current_chat, message))


class FakeWxSequence(FakeWx):
    def __init__(self, message_batches: list[list[FakeMessage]]) -> None:
        super().__init__(messages=[])
        self.message_batches = message_batches
        self.get_calls = 0

    def GetAllMessage(self) -> list[FakeMessage]:
        index = min(self.get_calls, len(self.message_batches) - 1)
        self.get_calls += 1
        return self.message_batches[index]


class FakeSession:
    def __init__(
        self,
        name: str = "chat",
        content: str = "hello",
        isnew: bool = False,
        new_count: int = 0,
    ) -> None:
        self.name = name
        self.content = content
        self.time = "2026-07-16 12:00:00"
        self.isnew = isnew
        self.new_count = new_count
        self.info = {
            "name": name,
            "time": self.time,
            "content": content,
            "isnew": isnew,
            "new_count": new_count,
        }


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
        message_buffer_seconds=3,
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

    def test_first_customer_message_sends_welcome_once_before_ai_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            chat = FakeChat()
            llm = FakeLLM(reply="ai reply")
            welcome = "welcome text"
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                    welcome_message_enabled=True,
                    welcome_message=welcome,
                ),
                llm_client=llm,
            )

            bot._on_message(FakeMessage("hello"), chat)
            bot._on_message(FakeMessage("second"), chat)

        self.assertEqual(chat.sent, [welcome, "ai reply"])
        self.assertEqual(llm.calls, 1)

    def test_visible_customer_message_sends_welcome_before_startup_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wx = FakeWx(messages=[FakeMessage(content="hello", sender="new customer")])
            welcome = "welcome text"
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    reply_allowed_chats=["*"],
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                    welcome_message_enabled=True,
                    welcome_message=welcome,
                ),
                llm_client=FakeLLM(),
            )
            seen: dict[str, set[str]] = {}

            bot._seed_polling_chat(wx, "new customer", seen)
            bot._seed_polling_chat(wx, "new customer", seen)

        self.assertEqual(wx.sent, [("new customer", welcome)])

    def test_startup_seed_does_not_welcome_when_latest_visible_message_is_self(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wx = FakeWx(
                messages=[
                    FakeMessage(content="hello", sender="new customer"),
                    FakeMessage(content="already replied", sender="self"),
                ]
            )
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    reply_allowed_chats=["*"],
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                    welcome_message_enabled=True,
                    welcome_message="welcome text",
                ),
                llm_client=FakeLLM(),
            )
            seen: dict[str, set[str]] = {}

            bot._seed_polling_chat(wx, "old customer", seen)

        self.assertEqual(wx.sent, [])

    def test_wildcard_allowed_chats_allows_any_chat(self) -> None:
        chat = FakeChat()
        llm = FakeLLM()
        bot = WeChatAutoReplyBot(
            config=make_config(reply_allowed_chats=["*"], dry_run=True),
            llm_client=llm,
        )

        bot._on_message(FakeMessage(), chat)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, [])

    def test_star_chat_enables_session_scan_mode(self) -> None:
        self.assertTrue(WeChatAutoReplyBot._is_session_scan_mode(["*"]))
        self.assertFalse(WeChatAutoReplyBot._is_session_scan_mode(["KiraAI"]))

    def test_group_like_chat_is_skipped_unless_explicitly_allowed(self) -> None:
        group_name = "\u6df1\u5ea6\u89c9\u9192\u5de5\u4f5c\u7fa4"
        bot = WeChatAutoReplyBot(
            config=make_config(
                reply_allowed_chats=["*"],
                group_chats_enabled=False,
                group_chat_patterns=["\u5de5\u4f5c\u7fa4"],
            ),
            llm_client=FakeLLM(),
        )
        explicit_bot = WeChatAutoReplyBot(
            config=make_config(
                reply_allowed_chats=[group_name],
                group_chats_enabled=False,
                group_chat_patterns=["\u5de5\u4f5c\u7fa4"],
            ),
            llm_client=FakeLLM(),
        )

        self.assertTrue(bot._should_skip_group_chat(group_name))
        self.assertFalse(explicit_bot._should_skip_group_chat(group_name))

    def test_club_named_contact_is_not_treated_as_group_by_default(self) -> None:
        bot = WeChatAutoReplyBot(
            config=make_config(
                reply_allowed_chats=["*"],
                group_chats_enabled=False,
                group_chat_patterns=["\u7fa4\u804a", "\u5fae\u4fe1\u7fa4", "\u5de5\u4f5c\u7fa4"],
            ),
            llm_client=FakeLLM(),
        )

        self.assertFalse(bot._should_skip_group_chat("\u963f\u7389\u4e09\u53cb\u8d22\u5bcc\u4ff1\u4e50\u90e8"))

    def test_session_helpers_read_visible_chat_info(self) -> None:
        session = FakeSession(name="KiraAI", content="latest", isnew=True, new_count=2)

        self.assertEqual(WeChatAutoReplyBot._session_name(session), "KiraAI")
        self.assertIn("latest", WeChatAutoReplyBot._session_signature(session))
        self.assertTrue(WeChatAutoReplyBot._session_has_unread(session))

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

    def test_voice_message_is_transcribed_before_reply(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="voice reply")
        voice = FakeVoiceMessage("what is the price")
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False),
            llm_client=llm,
        )

        bot._on_message(voice, chat)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, ["voice reply"])
        self.assertEqual(llm.histories[-1][-1]["content"], "what is the price")
        self.assertEqual(voice.to_text_calls, 1)

    def test_unplayed_voice_placeholder_is_transcribed_before_reply(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="voice reply")
        voice = FakeVoiceMessage(
            "what is the price",
            content='语音5"秒未播放',
            msg_type="text",
        )
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False),
            llm_client=llm,
        )

        bot._on_message(voice, chat)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, ["voice reply"])
        self.assertEqual(llm.histories[-1][-1]["content"], "what is the price")
        self.assertEqual(voice.to_text_calls, 1)

    def test_real_chinese_voice_placeholder_is_transcribed_before_reply(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="voice reply")
        voice = FakeVoiceMessage(
            "what is the price",
            content='\u8bed\u97f35"\u79d2\u672a\u64ad\u653e',
            msg_type="text",
        )
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False),
            llm_client=llm,
        )

        bot._on_message(voice, chat)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, ["voice reply"])
        self.assertEqual(llm.histories[-1][-1]["content"], "what is the price")
        self.assertEqual(voice.to_text_calls, 1)

    def test_voice_message_is_skipped_when_transcription_is_disabled(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="voice reply")
        voice = FakeVoiceMessage("what is the price")
        bot = WeChatAutoReplyBot(
            config=make_config(
                dry_run=False,
                require_manual_confirm=False,
                voice_to_text_enabled=False,
            ),
            llm_client=llm,
        )

        bot._on_message(voice, chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])
        self.assertEqual(voice.to_text_calls, 0)

    def test_voice_message_is_skipped_when_transcription_fails(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="voice reply")
        voice = FakeBrokenVoiceMessage("what is the price")
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False),
            llm_client=llm,
        )

        bot._on_message(voice, chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])
        self.assertEqual(voice.to_text_calls, 1)

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

    def test_live_send_splits_multiline_reply_with_delay(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="first\nsecond\n\nthird")
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.time.sleep") as sleep:
            bot._on_message(FakeMessage(), chat)

        self.assertEqual(chat.sent, ["first", "second", "third"])
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(2.5)
        self.assertEqual(bot.sent_count, 1)

    def test_live_send_splits_long_sentence_reply_with_delay(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(
            reply=(
                "我们主要做两个方向：一个是AI智能前台和客服平台，帮企业自动接待访客、回答常见问题；"
                "另一个是AI虚拟试穿，用户上传照片就能试婚纱或服装。"
                "你具体关心哪个场景？我可以让对应的同事详细介绍。"
            )
        )
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.time.sleep") as sleep:
            bot._on_message(FakeMessage(), chat)

        self.assertGreater(len(chat.sent), 1)
        self.assertEqual("".join(chat.sent), llm.reply)
        self.assertEqual(sleep.call_count, len(chat.sent) - 1)

    def test_split_reply_merges_tiny_tail(self) -> None:
        llm = FakeLLM()
        bot = WeChatAutoReplyBot(
            config=make_config(max_reply_chars=120),
            llm_client=llm,
        )

        parts = bot._split_reply_parts(
            "这套系统可以帮企业自动接待客户、回答常见问题、收集线索，并且把关键需求同步给销售同事。"
            "如果你们现在咨询量比较大，它还能把客户按意向分类，方便后续跟进。"
            "要不要我先按你的行业给你举个例子？"
        )

        self.assertGreater(len(parts), 1)
        self.assertGreaterEqual(len(parts[-1]), 12)

    def test_message_buffer_waits_then_combines_customer_messages(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="combined")
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False, message_buffer_seconds=3),
            llm_client=llm,
        )
        pending: dict[str, dict[str, object]] = {}

        with patch("wechat_ai.bot.time.time", side_effect=[100.0, 101.0, 103.9, 104.1, 104.1, 104.1]):
            bot._queue_buffered_message(pending, "chat", chat, FakeMessage("第一句"))
            bot._queue_buffered_message(pending, "chat", chat, FakeMessage("第二句"))
            bot._flush_due_message_buffers(pending)
            self.assertEqual(llm.calls, 0)
            bot._flush_due_message_buffers(pending)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, ["combined"])
        self.assertEqual(llm.histories[-1][-1]["content"], "第一句\n第二句")

    def test_message_buffer_combines_voice_and_text(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="combined")
        voice = FakeVoiceMessage("voice part")
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False, message_buffer_seconds=3),
            llm_client=llm,
        )
        pending: dict[str, dict[str, object]] = {}

        with patch("wechat_ai.bot.time.time", side_effect=[100.0, 101.0, 104.1, 104.1, 104.1]):
            bot._queue_buffered_message(pending, "chat", chat, voice)
            bot._queue_buffered_message(pending, "chat", chat, FakeMessage("text part"))
            bot._flush_due_message_buffers(pending)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, ["combined"])
        self.assertEqual(llm.histories[-1][-1]["content"], "voice part\ntext part")
        self.assertEqual(voice.to_text_calls, 1)

    def test_message_buffer_flushes_one_chat_per_tick(self) -> None:
        chat_a = FakeChat()
        chat_b = FakeChat()
        chat_b.who = "chat-b"
        llm = FakeLLM(reply="reply")
        bot = WeChatAutoReplyBot(
            config=make_config(
                reply_allowed_chats=["*"],
                dry_run=False,
                require_manual_confirm=False,
                message_buffer_seconds=3,
            ),
            llm_client=llm,
        )
        pending: dict[str, dict[str, object]] = {}

        with patch("wechat_ai.bot.time.time", side_effect=[100.0, 100.0, 104.0, 104.0, 104.0]):
            bot._queue_buffered_message(pending, "chat", chat_a, FakeMessage("one"))
            bot._queue_buffered_message(pending, "chat-b", chat_b, FakeMessage("two"))
            bot._flush_due_message_buffers(pending)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat_a.sent, ["reply"])
        self.assertEqual(chat_b.sent, [])
        self.assertIn("chat-b", pending)

    def test_poll_chat_rechecks_after_handling_interleaved_message(self) -> None:
        wx = FakeWxSequence(
            [
                [FakeMessage("old", sender="alice")],
                [
                    FakeMessage("old", sender="alice"),
                    FakeMessage("new while sending", sender="alice"),
                ],
            ]
        )
        llm = FakeLLM(reply="reply")
        bot = WeChatAutoReplyBot(
            config=make_config(
                dry_run=False,
                require_manual_confirm=False,
                message_buffer_seconds=3,
            ),
            llm_client=llm,
        )
        seen: dict[str, set[str]] = {}
        pending: dict[str, dict[str, object]] = {}

        bot._poll_chat_once(wx, "chat", seen, pending)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(wx.get_calls, 2)
        self.assertIn("chat", pending)
        messages = pending["chat"]["messages"]
        self.assertEqual([msg.content for msg in messages], ["old", "new while sending"])

    def test_message_buffer_retries_voice_transcription_before_reply(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="combined")
        voice = FakeFlakyVoiceMessage("voice part")
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False, message_buffer_seconds=3),
            llm_client=llm,
        )
        pending: dict[str, dict[str, object]] = {}

        bot._queue_buffered_message(pending, "chat", chat, voice)
        bot._flush_message_buffer(pending, "chat")

        self.assertIn("chat", pending)
        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

        bot._flush_message_buffer(pending, "chat")

        self.assertNotIn("chat", pending)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, ["combined"])
        self.assertEqual(llm.histories[-1][-1]["content"], "voice part")
        self.assertEqual(voice.to_text_calls, 3)

    def test_message_buffer_delays_during_cooldown_without_dropping_message(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="after cooldown")
        bot = WeChatAutoReplyBot(
            config=make_config(
                dry_run=False,
                require_manual_confirm=False,
                message_buffer_seconds=3,
                reply_cooldown_seconds=3,
            ),
            llm_client=llm,
        )
        pending: dict[str, dict[str, object]] = {}

        with patch("wechat_ai.bot.time.time", return_value=100.0):
            bot.last_reply_at["chat"] = 98.0
            bot._queue_buffered_message(pending, "chat", chat, FakeMessage("question"))
            bot._flush_message_buffer(pending, "chat")

        self.assertIn("chat", pending)
        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

        with patch("wechat_ai.bot.time.time", return_value=105.0):
            bot._flush_message_buffer(pending, "chat")

        self.assertNotIn("chat", pending)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, ["after cooldown"])

    def test_seed_history_from_visible_messages(self) -> None:
        llm = FakeLLM()
        bot = WeChatAutoReplyBot(config=make_config(), llm_client=llm)

        bot._seed_history_from_messages(
            "chat",
            [
                FakeMessage("09:00", sender="", msg_type="time"),
                FakeMessage("你们公司是干什么的", sender="alice"),
                FakeMessage("我们做AI客服。", sender="self"),
            ],
        )

        self.assertEqual(
            list(bot.histories["chat"]),
            [
                {"role": "user", "sender": "alice", "content": "你们公司是干什么的"},
                {"role": "assistant", "sender": "self", "content": "我们做AI客服。"},
            ],
        )

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
