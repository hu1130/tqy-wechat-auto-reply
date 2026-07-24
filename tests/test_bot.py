from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import tempfile
import time
from threading import Event, Thread
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_ai.bot import LLM_ERROR_FALLBACK_REPLY, WeChatAutoReplyBot
from wechat_ai.config import AppConfig
from wechat_ai.wechat_runtime import OperationRejectedError, ReconnectBackoff


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
    def __init__(
        self,
        content: str = "hello",
        sender: str = "alice",
        msg_type: str = "text",
        msg_id: str = "",
    ) -> None:
        self.content = content
        self.sender = sender
        self.type = msg_type
        self.id = msg_id or None


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


class FakeListenerChat(FakeChat):
    def __init__(self, who: str, messages: list[FakeMessage]) -> None:
        super().__init__()
        self.who = who
        self.messages = messages

    def GetAllMessage(self) -> list[FakeMessage]:
        return self.messages


class FakeListenerWx:
    def __init__(self, old_message: FakeMessage, new_message: FakeMessage) -> None:
        self.chat = FakeListenerChat("chat", [old_message])
        self.old_message = old_message
        self.new_message = new_message
        self.callback = None

    def AddListenChat(self, chat_name: str, callback: object) -> FakeListenerChat:
        self.chat.who = chat_name
        self.callback = callback
        callback(self.old_message, self.chat)
        return self.chat

    def KeepRunning(self) -> None:
        assert self.callback is not None
        self.callback(self.old_message, self.chat)
        self.callback(self.new_message, self.chat)


class FakeUnreadableListenerWx:
    def __init__(self) -> None:
        self.keep_running_called = False

    def AddListenChat(self, chat_name: str, callback: object) -> FakeChat:
        return FakeChat()

    def KeepRunning(self) -> None:
        self.keep_running_called = True


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


class FakeFlakySendWx(FakeWx):
    def __init__(self) -> None:
        super().__init__()
        self.send_calls = 0
        self.chat_with_calls = 0

    def ChatWith(self, chat_name: str) -> None:
        self.chat_with_calls += 1
        super().ChatWith(chat_name)

    def SendMsg(self, message: str) -> None:
        self.send_calls += 1
        if self.send_calls == 1:
            raise RuntimeError("uia send failed")
        super().SendMsg(message)


class FakeForceChatWx(FakeWx):
    def __init__(self) -> None:
        super().__init__()
        self.chat_with_kwargs: list[dict[str, object]] = []

    def ChatWith(self, chat_name: str, **kwargs: object) -> None:
        self.chat_with_kwargs.append(kwargs)
        super().ChatWith(chat_name)


class FakeAlwaysFailChat(FakeChat):
    def SendMsg(self, message: str) -> None:
        raise RuntimeError("send failed")


class FakeWxSequence(FakeWx):
    def __init__(self, message_batches: list[list[FakeMessage]]) -> None:
        super().__init__(messages=[])
        self.message_batches = message_batches
        self.get_calls = 0

    def GetAllMessage(self) -> list[FakeMessage]:
        index = min(self.get_calls, len(self.message_batches) - 1)
        self.get_calls += 1
        return self.message_batches[index]


class FakeStartupPollingWx(FakeWx):
    def __init__(self, old_message: FakeMessage, new_message: FakeMessage) -> None:
        super().__init__()
        self.old_message = old_message
        self.new_message = new_message
        self.get_calls = 0

    def GetAllMessage(self) -> list[FakeMessage]:
        self.get_calls += 1
        if self.get_calls == 1:
            return [self.old_message]
        if self.get_calls == 2:
            return [self.old_message, self.new_message]
        raise KeyboardInterrupt


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


class FakeSessionScanWx(FakeWx):
    def __init__(self) -> None:
        super().__init__()
        self.session_calls = 0
        self.messages_by_chat = {
            "existing": [FakeMessage("existing old", msg_id="existing-old")],
            "discovered": [FakeMessage("discovered old", msg_id="discovered-old")],
        }

    def GetSession(self) -> list[FakeSession]:
        self.session_calls += 1
        if self.session_calls == 1:
            return [FakeSession(name="existing", content="existing old")]
        if self.session_calls == 2:
            return [
                FakeSession(name="existing", content="existing old"),
                FakeSession(name="discovered", content="discovered old", isnew=True, new_count=1),
            ]
        if self.session_calls == 3:
            self.messages_by_chat["discovered"].append(
                FakeMessage("truly new", msg_id="discovered-new")
            )
            return [
                FakeSession(name="existing", content="existing old"),
                FakeSession(name="discovered", content="truly new", isnew=True, new_count=1),
            ]
        raise KeyboardInterrupt

    def GetAllMessage(self) -> list[FakeMessage]:
        return self.messages_by_chat[self.current_chat]


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
        operator_idle_guard_seconds=0,
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

    def test_inbound_message_is_persisted_before_llm_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            message = FakeMessage("persist first", msg_id="persist-before-llm")
            observed_statuses: list[str] = []

            class InspectingLLM(FakeLLM):
                def generate_reply(
                    self,
                    chat_name: str,
                    history: list[dict[str, str]],
                    knowledge_context: str = "",
                ) -> str:
                    assert bot.store is not None
                    fingerprint = bot._message_fingerprint(message, chat_name)
                    stored = bot.store.get_message_by_fingerprint(fingerprint)
                    observed_statuses.append(stored.status if stored else "missing")
                    return super().generate_reply(chat_name, history, knowledge_context)

            llm = InspectingLLM(reply="persisted reply")
            bot = WeChatAutoReplyBot(
                config=make_config(state_db_file=str(db_path)),
                llm_client=llm,
            )

            bot._on_message(message, FakeChat())
            stored = bot.store.get_message_by_fingerprint(
                bot._message_fingerprint(message, "chat")
            ) if bot.store else None

        self.assertEqual(observed_statuses, ["processing"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "generated")

    def test_persisted_llm_failure_marks_message_failed_without_fallback_send(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            chat = FakeChat()
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(db_path),
                ),
                llm_client=FakeLLM(error=RuntimeError("LLM unavailable")),
            )
            message = FakeMessage("generation fails", msg_id="llm-failure")

            bot._on_message(message, chat)
            stored = bot.store.get_message_by_fingerprint(
                bot._message_fingerprint(message, "chat")
            ) if bot.store else None

        self.assertEqual(chat.sent, [])
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "failed")
        self.assertIn("llm_error", stored.error)

    def test_persisted_message_id_is_deduplicated_after_bot_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            message = FakeMessage("same message", msg_id="stable-wechat-id")
            first_llm = FakeLLM(reply="first reply")
            first_bot = WeChatAutoReplyBot(
                config=make_config(state_db_file=str(db_path)),
                llm_client=first_llm,
            )
            first_bot._on_message(message, FakeChat())

            second_llm = FakeLLM(reply="must not run")
            second_bot = WeChatAutoReplyBot(
                config=make_config(state_db_file=str(db_path)),
                llm_client=second_llm,
            )
            second_bot._on_message(message, FakeChat())

        self.assertEqual(first_llm.calls, 1)
        self.assertEqual(second_llm.calls, 0)

    def test_same_content_with_distinct_message_ids_is_not_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            chat = FakeChat()
            llm = FakeLLM(reply="answer")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                ),
                llm_client=llm,
            )

            bot._on_message(FakeMessage("same content", msg_id="message-1"), chat)
            bot._on_message(FakeMessage("same content", msg_id="message-2"), chat)

        self.assertEqual(llm.calls, 2)
        self.assertEqual(chat.sent, ["answer", "answer"])

    def test_live_reply_is_processing_until_atomic_outbox_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                ),
                llm_client=FakeLLM(reply="atomic answer"),
            )
            assert bot.store is not None
            original_enqueue = bot.store.enqueue_outbox
            observed_statuses: list[str] = []
            message = FakeMessage("atomic question", msg_id="atomic-source")
            fingerprint = bot._message_fingerprint(message, "chat")

            def inspect_enqueue(*args: object, **kwargs: object):
                stored = bot.store.get_message_by_fingerprint(fingerprint)
                observed_statuses.append(stored.status if stored else "missing")
                return original_enqueue(*args, **kwargs)

            with patch.object(bot.store, "enqueue_outbox", side_effect=inspect_enqueue):
                bot._on_message(message, FakeChat())

        self.assertEqual(observed_statuses, ["processing"])

    def test_persistence_failure_stops_before_ai_and_send(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            chat = FakeChat()
            llm = FakeLLM(reply="must not run")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                ),
                llm_client=llm,
            )
            assert bot.store is not None

            with patch.object(bot.store, "record_message", side_effect=RuntimeError("disk unavailable")):
                with self.assertRaisesRegex(RuntimeError, "Failed to persist inbound message"):
                    bot._on_message(FakeMessage("do not lose me", msg_id="db-failure"), chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

    def test_message_fingerprint_uses_stable_fields_without_object_identity(self) -> None:
        bot = WeChatAutoReplyBot(config=make_config(), llm_client=FakeLLM())
        first = FakeMessage("same", sender="alice")
        second = FakeMessage("same", sender="alice")
        first.time = "2026-07-21 10:00:00"
        second.time = "2026-07-21 10:00:00"
        first.attr = object()
        second.attr = object()

        first_fingerprint = bot._message_fingerprint(first, "chat")
        second_fingerprint = bot._message_fingerprint(second, "chat")

        self.assertEqual(first_fingerprint, second_fingerprint)
        self.assertTrue(first_fingerprint.startswith("wechat:sha256:"))
        self.assertNotIn("0x", first_fingerprint)

        unsupported_first = FakeMessage()
        unsupported_second = FakeMessage()
        unsupported_first.content = object()
        unsupported_second.content = object()
        self.assertEqual(
            bot._message_fingerprint(unsupported_first, "chat"),
            bot._message_fingerprint(unsupported_second, "chat"),
        )

        first.message_id = "preferred-id"
        first.content = "changed"
        preferred_fingerprint = bot._message_fingerprint(first, "another chat")
        self.assertTrue(preferred_fingerprint.startswith("wechat:id:"))
        self.assertNotEqual(preferred_fingerprint, bot._message_fingerprint(first, "chat"))

    def test_buffered_raw_message_is_persisted_before_voice_ai(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            voice = FakeVoiceMessage("transcribed later")
            voice.id = "buffered-voice-id"
            bot = WeChatAutoReplyBot(
                config=make_config(state_db_file=str(db_path)),
                llm_client=FakeLLM(),
            )
            pending: dict[str, dict[str, object]] = {}

            bot._queue_buffered_message(pending, "chat", FakeChat(), voice)
            stored = bot.store.get_message_by_fingerprint(
                bot._message_fingerprint(voice, "chat")
            ) if bot.store else None

        self.assertEqual(voice.to_text_calls, 0)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "received")
        self.assertEqual(len(pending["chat"]["message_ids"]), 1)

    def test_live_outbox_is_sending_before_send_and_finishes_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            sending_statuses: list[str] = []
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(db_path),
                ),
                llm_client=FakeLLM(reply="outbox reply"),
            )

            class InspectingChat(FakeChat):
                def SendMsg(self, message: str) -> None:
                    assert bot.store is not None
                    jobs = bot.store.list_outbox(status="sending")
                    sending_statuses.append(jobs[0].status if jobs else "missing")
                    super().SendMsg(message)

            chat = InspectingChat()
            message = FakeMessage("outbox question", msg_id="outbox-success")
            bot._on_message(message, chat)
            sent_jobs = bot.store.list_outbox(status="sent") if bot.store else []
            stored = bot.store.get_message_by_fingerprint(
                bot._message_fingerprint(message, "chat")
            ) if bot.store else None

        self.assertEqual(sending_statuses, ["sending"])
        self.assertEqual(chat.sent, ["outbox reply"])
        self.assertEqual(len(sent_jobs), 1)
        self.assertEqual(sent_jobs[0].attempts, 1)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "sent")

    def test_queued_outbox_is_sent_after_bot_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            assert first_bot.store is not None
            stored, created = first_bot.store.record_message(
                fingerprint="wechat:id:queued-source",
                chat_name="chat",
                sender="alice",
                msg_type="text",
                content="queued question",
            )
            self.assertTrue(created)
            first_bot.store.mark_messages_processing([stored.id])
            first_bot.store.enqueue_outbox(
                message_ids=[stored.id],
                chat_name="chat",
                sender="alice",
                msg_type="text",
                policy="auto",
                content="queued question",
                reply="queued reply",
            )

            second_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            wx = FakeWx()
            second_bot._recover_and_drain_outbox(wx)
            sent_jobs = second_bot.store.list_outbox(status="sent") if second_bot.store else []

        self.assertEqual(wx.sent, [("chat", "queued reply")])
        self.assertEqual(len(sent_jobs), 1)

    def test_interrupted_sending_outbox_becomes_uncertain_without_resend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
                outbox_uncertain_retry_enabled=False,
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            assert first_bot.store is not None
            stored, _ = first_bot.store.record_message(
                fingerprint="wechat:id:interrupted-source",
                chat_name="chat",
                sender="alice",
                msg_type="text",
                content="interrupted question",
            )
            first_bot.store.mark_messages_processing([stored.id])
            job = first_bot.store.enqueue_outbox(
                message_ids=[stored.id],
                chat_name="chat",
                sender="alice",
                msg_type="text",
                policy="auto",
                content="interrupted question",
                reply="do not retry",
            )
            claimed = first_bot.store.claim_outbox(job.id)
            self.assertIsNotNone(claimed)

            second_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            wx = FakeWx()
            second_bot._recover_and_drain_outbox(wx)
            uncertain = second_bot.store.list_outbox(status="uncertain") if second_bot.store else []

        self.assertEqual(wx.sent, [])
        self.assertEqual(len(uncertain), 1)
        self.assertIn("Interrupted while sending", uncertain[0].error)

    def test_received_and_processing_messages_resume_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            assert first_bot.store is not None
            received, _ = first_bot.store.record_message(
                "wechat:id:recover-received", "chat", "alice", "text", "first"
            )
            processing, _ = first_bot.store.record_message(
                "wechat:id:recover-processing", "chat", "alice", "text", "second"
            )
            first_bot.store.mark_messages_processing([processing.id])

            second_llm = FakeLLM(reply="recovered")
            second_bot = WeChatAutoReplyBot(config=config, llm_client=second_llm)
            wx = FakeWx()
            second_bot._recover_and_drain_outbox(wx)
            assert second_bot.store is not None
            received_after = second_bot.store.get_message(received.id)
            processing_after = second_bot.store.get_message(processing.id)

        self.assertEqual(second_llm.calls, 2)
        self.assertEqual(wx.sent, [("chat", "recovered"), ("chat", "recovered")])
        self.assertEqual(received_after.status, "sent")
        self.assertEqual(processing_after.status, "sent")

    def test_dry_run_startup_does_not_drain_live_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            live_config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
            )
            first_bot = WeChatAutoReplyBot(config=live_config, llm_client=FakeLLM())
            assert first_bot.store is not None
            stored, _ = first_bot.store.record_message(
                fingerprint="wechat:id:dry-run-queued-source",
                chat_name="chat",
                sender="alice",
                msg_type="text",
                content="queued before dry run",
            )
            first_bot.store.mark_messages_processing([stored.id])
            first_bot.store.enqueue_outbox(
                message_ids=[stored.id],
                chat_name="chat",
                sender="alice",
                msg_type="text",
                policy="auto",
                content="queued before dry run",
                reply="must remain queued",
            )

            dry_bot = WeChatAutoReplyBot(
                config=live_config.with_overrides(dry_run=True),
                llm_client=FakeLLM(),
            )
            wx = FakeWx()
            dry_bot._recover_and_drain_outbox(wx)
            queued = dry_bot.store.list_outbox(status="queued") if dry_bot.store else []

        self.assertEqual(wx.sent, [])
        self.assertEqual(len(queued), 1)

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
            sent_jobs = bot.store.list_outbox(status="sent") if bot.store else []

        self.assertEqual(chat.sent, [welcome, "ai reply"])
        self.assertEqual(llm.calls, 1)
        self.assertEqual([job.kind for job in sent_jobs], ["welcome", "reply"])

    def test_concurrent_first_messages_send_only_one_welcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            chat = FakeChat()
            llm = FakeLLM(reply="ai reply")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                    welcome_message_enabled=True,
                    welcome_message="welcome text",
                ),
                llm_client=llm,
            )

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(
                    executor.map(
                        lambda message: bot._on_message(message, chat),
                        (
                            FakeMessage("first", msg_id="welcome-1"),
                            FakeMessage("second", msg_id="welcome-2"),
                        ),
                    )
                )

        self.assertEqual(chat.sent.count("welcome text"), 1)
        self.assertEqual(chat.sent.count("ai reply"), 1)

    def test_listener_replay_is_baselined_before_new_messages_are_processed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_message = FakeMessage("old replay", msg_id="old")
            new_message = FakeMessage("new after startup", msg_id="new")
            wx = FakeListenerWx(old_message, new_message)
            llm = FakeLLM(reply="new reply")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    message_buffer_seconds=0,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                ),
                llm_client=llm,
            )

            with patch.object(bot, "_create_wechat_client", return_value=wx):
                bot.start()

        self.assertEqual(llm.calls, 1)
        self.assertEqual(wx.chat.sent, ["new reply"])
        self.assertEqual(
            [item["content"] for item in bot.histories["chat"]],
            ["old replay", "new after startup", "new reply"],
        )

    def test_live_start_requires_persistent_state_database(self) -> None:
        bot = WeChatAutoReplyBot(
            config=make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file="",
            ),
            llm_client=FakeLLM(),
        )

        with patch.object(bot, "_create_wechat_client") as create_wechat:
            with self.assertRaisesRegex(RuntimeError, "requires STATE_DB_FILE"):
                bot.start()

        create_wechat.assert_not_called()

    def test_listener_without_readable_history_stops_before_running(self) -> None:
        wx = FakeUnreadableListenerWx()
        llm = FakeLLM()
        bot = WeChatAutoReplyBot(config=make_config(), llm_client=llm)

        with patch.object(bot, "_create_wechat_client", return_value=wx):
            with self.assertRaisesRegex(RuntimeError, "startup baseline"):
                bot.start()

        self.assertEqual(llm.calls, 0)
        self.assertFalse(wx.keep_running_called)

    def test_fixed_chat_polling_seeds_old_messages_then_processes_new_message(self) -> None:
        wx = FakeStartupPollingWx(
            FakeMessage("old", msg_id="old"),
            FakeMessage("new", msg_id="new"),
        )
        llm = FakeLLM(reply="new reply")
        bot = WeChatAutoReplyBot(
            config=make_config(
                dry_run=False,
                require_manual_confirm=False,
                message_buffer_seconds=0,
            ),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.time.sleep", return_value=None):
            with self.assertRaises(KeyboardInterrupt):
                bot._start_polling(wx, ["chat"])

        self.assertEqual(llm.calls, 1)
        self.assertEqual(wx.sent, [("chat", "new reply")])

    def test_session_scan_first_discovery_only_seeds_then_processes_later_message(self) -> None:
        wx = FakeSessionScanWx()
        llm = FakeLLM(reply="new reply")
        bot = WeChatAutoReplyBot(
            config=make_config(
                wechat_listen_chats=["*"],
                reply_allowed_chats=["*"],
                dry_run=False,
                require_manual_confirm=False,
                message_buffer_seconds=0,
            ),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.time.sleep", return_value=None):
            with self.assertRaises(KeyboardInterrupt):
                bot._start_session_scan_polling(wx)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(wx.sent, [("discovered", "new reply")])
        self.assertEqual(
            [item["content"] for item in bot.histories["discovered"]],
            ["discovered old", "truly new", "new reply"],
        )

    def test_visible_customer_message_does_not_send_welcome_during_startup_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wx = FakeWx(messages=[FakeMessage(content="hello", sender="new customer")])
            welcome = "welcome text"
            llm = FakeLLM()
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    reply_allowed_chats=["*"],
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                    welcome_message_enabled=True,
                    welcome_message=welcome,
                ),
                llm_client=llm,
            )
            seen: dict[str, set[str]] = {}

            bot._seed_polling_chat(wx, "new customer", seen)
            bot._seed_polling_chat(wx, "new customer", seen)

        self.assertEqual(wx.sent, [])
        self.assertEqual(llm.calls, 0)
        self.assertEqual([item["content"] for item in bot.histories["new customer"]], ["hello"])

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

    def test_operator_idle_guard_detects_recent_activity(self) -> None:
        bot = WeChatAutoReplyBot(
            config=make_config(operator_idle_guard_seconds=8),
            llm_client=FakeLLM(),
        )

        with patch("wechat_ai.bot.platform.system", return_value="Windows"):
            with patch.object(bot, "_windows_idle_seconds", return_value=2.0):
                self.assertTrue(bot._operator_recently_active())
            with patch.object(bot, "_windows_idle_seconds", return_value=10.0):
                self.assertFalse(bot._operator_recently_active())

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

    def test_non_text_message_type_skips_llm_and_send_when_image_disabled(self) -> None:
        chat = FakeChat()
        llm = FakeLLM()
        bot = WeChatAutoReplyBot(config=make_config(image_understanding_enabled=False), llm_client=llm)

        bot._on_message(FakeMessage(msg_type="image"), chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

    def test_image_message_is_passed_to_llm_with_safe_context(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="please describe the image")
        bot = WeChatAutoReplyBot(
            config=make_config(image_screenshot_fallback_enabled=True),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.resolve_recent_wechat_image") as resolver:
            resolver.return_value.found = False
            resolver.return_value.status = "missing"
            resolver.return_value.reason = "no cache"
            bot._on_message(FakeMessage(content="image", msg_type="image"), chat)

        self.assertEqual(llm.calls, 1)
        self.assertIn("图片", llm.histories[-1][-1]["content"])

    def test_image_description_is_passed_to_llm_when_vision_succeeds(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="image reply")
        bot = WeChatAutoReplyBot(
            config=make_config(image_screenshot_fallback_enabled=True),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.resolve_recent_wechat_image") as resolver:
            resolver.return_value.found = True
            resolver.return_value.path = r"C:\tmp\image.png"
            with patch.object(bot.vision_client, "is_configured", return_value=True):
                with patch.object(bot.vision_client, "describe_image", return_value="vision table text"):
                    bot._on_message(FakeMessage(content="image", msg_type="image"), chat)

        self.assertEqual(llm.calls, 1)
        self.assertIn("vision table text", llm.histories[-1][-1]["content"])

    def test_localized_image_placeholder_is_treated_as_image(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="image reply")
        bot = WeChatAutoReplyBot(
            config=make_config(image_screenshot_fallback_enabled=True),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.resolve_recent_wechat_image") as resolver:
            resolver.return_value.found = True
            resolver.return_value.path = r"C:\tmp\wechat-localized-image.png"
            with patch.object(bot.vision_client, "is_configured", return_value=True):
                with patch.object(bot.vision_client, "describe_image", return_value="localized image text"):
                    bot._on_message(FakeMessage(content="[图片]", msg_type="text"), chat)

        self.assertEqual(llm.calls, 1)
        self.assertIn("localized image text", llm.histories[-1][-1]["content"])

    def test_unclear_cached_image_tries_screenshot_fallback(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="image reply")
        bot = WeChatAutoReplyBot(
            config=make_config(image_screenshot_fallback_enabled=True),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.resolve_recent_wechat_image") as resolver:
            resolver.return_value.found = True
            resolver.return_value.path = r"C:\tmp\small-cache.png"
            with patch.object(bot.vision_client, "is_configured", return_value=True):
                with patch.object(
                    bot.vision_client,
                    "describe_image",
                    side_effect=["图片关键信息看不清", "opened preview text"],
                ):
                    with patch("wechat_ai.bot.open_visible_wechat_image_preview", return_value=True):
                        with patch("wechat_ai.bot.capture_foreground_window_image") as capture:
                            capture.return_value.found = False
                            capture.return_value.status = "screenshot"
                            capture.return_value.path = r"C:\tmp\wechat-window.png"
                            bot._on_message(FakeMessage(content="image", msg_type="image"), chat)

        self.assertEqual(llm.calls, 1)
        self.assertIn("opened preview text", llm.histories[-1][-1]["content"])

    def test_image_uses_window_screenshot_when_cache_path_is_missing(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="screenshot reply")
        bot = WeChatAutoReplyBot(
            config=make_config(image_screenshot_fallback_enabled=True),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.resolve_recent_wechat_image") as resolver:
            resolver.return_value.found = False
            resolver.return_value.status = "missing"
            resolver.return_value.reason = "no cache"
            with patch("wechat_ai.bot.capture_foreground_window_image") as capture:
                with patch("wechat_ai.bot.open_visible_wechat_image_preview", return_value=False):
                    capture.return_value.found = False
                    capture.return_value.status = "screenshot"
                    capture.return_value.path = r"C:\tmp\wechat-window.png"
                    with patch.object(bot.vision_client, "is_configured", return_value=True):
                        with patch.object(bot.vision_client, "describe_image", return_value="screenshot table text"):
                            bot._on_message(FakeMessage(content="image", msg_type="image"), chat)

        self.assertEqual(llm.calls, 1)
        self.assertIn("screenshot table text", llm.histories[-1][-1]["content"])

    def test_image_opens_preview_before_screenshot_fallback(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="screenshot reply")
        bot = WeChatAutoReplyBot(
            config=make_config(image_screenshot_fallback_enabled=True),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.resolve_recent_wechat_image") as resolver:
            resolver.return_value.found = False
            resolver.return_value.status = "missing"
            resolver.return_value.reason = "no cache"
            with patch("wechat_ai.bot.open_visible_wechat_image_preview", return_value=True):
                with patch("wechat_ai.bot.capture_foreground_window_image") as capture:
                    capture.return_value.found = False
                    capture.return_value.status = "screenshot"
                    capture.return_value.path = r"C:\tmp\wechat-window.png"
                    with patch.object(bot.vision_client, "is_configured", return_value=True):
                        with patch.object(bot.vision_client, "describe_image", return_value="preview table text"):
                            bot._on_message(FakeMessage(content="image", msg_type="image"), chat)

        capture.assert_called_once_with(enhance=False)
        self.assertIn("preview table text", llm.histories[-1][-1]["content"])

    def test_image_does_not_use_window_screenshot_by_default(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="image fallback reply")
        bot = WeChatAutoReplyBot(config=make_config(), llm_client=llm)

        with patch("wechat_ai.bot.resolve_recent_wechat_image") as resolver:
            resolver.return_value.found = False
            resolver.return_value.status = "missing"
            resolver.return_value.reason = "no cache"
            with patch("wechat_ai.bot.capture_foreground_window_image") as capture:
                bot._on_message(FakeMessage(content="image", msg_type="image"), chat)

        capture.assert_not_called()
        self.assertIn("图片", llm.histories[-1][-1]["content"])


    def test_llm_error_sends_fallback_reply(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(error=RuntimeError("HTTP 500"))
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False, max_errors_before_pause=0),
            llm_client=llm,
        )

        bot._on_message(FakeMessage(), chat)

        self.assertEqual(chat.sent, [LLM_ERROR_FALLBACK_REPLY])

    def test_polling_chat_uses_force_exact_chat_switch_before_send(self) -> None:
        from wechat_ai.bot import _PollingChat

        wx = FakeForceChatWx()
        chat = _PollingChat(wx, "chat", switch_before_send=True)

        with patch("wechat_ai.bot.time.sleep"):
            chat.SendMsg("hello")

        self.assertEqual(wx.sent, [("chat", "hello")])
        self.assertEqual(wx.chat_with_kwargs[0], {"exact": True, "force": True, "force_wait": 0.2})

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
            config=make_config(sensitive_keywords=["secret-code"]),
            llm_client=llm,
        )

        bot._on_message(FakeMessage("secret-code is 1234"), chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

    def test_unsafe_reply_is_blocked_before_send(self) -> None:
        chat = FakeChat()
        llm = FakeLLM(reply="unsafe reply")
        bot = WeChatAutoReplyBot(
            config=make_config(
                dry_run=False,
                require_manual_confirm=False,
                blocked_reply_patterns=["unsafe"],
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

    def test_polling_chat_does_not_retry_after_send_exception(self) -> None:
        from wechat_ai.bot import _PollingChat

        wx = FakeFlakySendWx()
        chat = _PollingChat(wx, "chat", switch_before_send=True, retry_on_error=False)

        with patch("wechat_ai.bot.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "uia send failed"):
                chat.SendMsg("hello")

        self.assertEqual(wx.sent, [])
        self.assertEqual(wx.send_calls, 1)
        self.assertEqual(wx.chat_with_calls, 1)

    def test_polling_chat_keeps_legacy_retry_when_outbox_is_disabled(self) -> None:
        from wechat_ai.bot import _PollingChat

        wx = FakeFlakySendWx()
        chat = _PollingChat(wx, "chat", switch_before_send=True)

        with patch("wechat_ai.bot.time.sleep"):
            chat.SendMsg("hello")

        self.assertEqual(wx.sent, [("chat", "hello")])
        self.assertEqual(wx.send_calls, 2)
        self.assertGreaterEqual(wx.chat_with_calls, 2)

    def test_failed_send_is_uncertain_and_is_not_retried_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            first_bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(db_path),
                    outbox_uncertain_retry_enabled=False,
                ),
                llm_client=FakeLLM(reply="retry me"),
            )

            first_bot._on_message(
                FakeMessage("question", msg_id="uncertain-message"),
                FakeAlwaysFailChat(),
            )
            uncertain = first_bot.store.list_outbox(status="uncertain") if first_bot.store else []
            old_retries = first_bot.store.list_failed_replies() if first_bot.store else []

            second_bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(db_path),
                    outbox_uncertain_retry_enabled=False,
                ),
                llm_client=FakeLLM(reply="unused"),
            )
            wx = FakeWx()
            second_bot._recover_and_drain_outbox(wx)
            uncertain_after_restart = (
                second_bot.store.list_outbox(status="uncertain") if second_bot.store else []
            )

        self.assertEqual(len(uncertain), 1)
        self.assertEqual(uncertain[0].reply, "retry me")
        self.assertEqual(old_retries, [])
        self.assertEqual(wx.sent, [])
        self.assertEqual(len(uncertain_after_restart), 1)

    def test_failed_send_is_retried_up_to_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
                outbox_uncertain_retry_enabled=True,
                outbox_uncertain_max_attempts=2,
                outbox_uncertain_retry_delay_seconds=0,
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM(reply="retry once"))
            first_bot._on_message(
                FakeMessage("question", msg_id="bounded-retry"),
                FakeAlwaysFailChat(),
            )
            queued = first_bot.store.list_outbox(status="queued") if first_bot.store else []

            restarted = WeChatAutoReplyBot(config=config, llm_client=FakeLLM(reply="unused"))
            wx = FakeWx()
            restarted._recover_and_drain_outbox(wx)
            sent = restarted.store.list_outbox(status="sent") if restarted.store else []

        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].attempts, 1)
        self.assertEqual(wx.sent, [("chat", "retry once")])
        self.assertEqual(sent[0].attempts, 2)

    def test_outbox_retry_resumes_after_last_persisted_reply_part(self) -> None:
        class FailSecondPart(FakeChat):
            def SendMsg(self, message: str) -> None:
                if len(self.sent) == 1:
                    raise RuntimeError("second part result unknown")
                self.sent.append(message)

        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                outbox_uncertain_retry_enabled=True,
                outbox_uncertain_max_attempts=2,
                outbox_uncertain_retry_delay_seconds=0,
            )
            bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            assert bot.store is not None
            message, _ = bot.store.record_message("multipart-source", "chat", content="question")
            bot.store.mark_messages_processing([message.id])
            job = bot.store.enqueue_outbox(
                [message.id], "chat", "alice", "text", "auto", "question", "first\nsecond"
            )
            claimed = bot.store.claim_outbox(job.id)
            first_chat = FailSecondPart()
            with patch("wechat_ai.bot.time.sleep"):
                delivered = bot._deliver_outbox_job(claimed, first_chat)
            queued = bot.store.list_outbox(status="queued")[0]

            wx = FakeWx()
            bot._drain_outbox(wx)

        self.assertFalse(delivered)
        self.assertEqual(first_chat.sent, ["first"])
        self.assertEqual(queued.sent_parts, 1)
        self.assertEqual(wx.sent, [("chat", "second")])

    def test_sent_status_persistence_failure_becomes_uncertain_without_resend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM(reply="sent once"))
            assert first_bot.store is not None
            chat = FakeChat()

            with patch.object(
                first_bot.store,
                "mark_outbox_sent",
                side_effect=RuntimeError("database write failed"),
            ):
                first_bot._on_message(
                    FakeMessage("question", msg_id="sent-status-failure"),
                    chat,
                )

            uncertain = first_bot.store.list_outbox(status="uncertain")
            second_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM(reply="unused"))
            wx = FakeWx()
            second_bot._recover_and_drain_outbox(wx)

        self.assertEqual(chat.sent, ["sent once"])
        self.assertEqual(len(uncertain), 1)
        self.assertEqual(wx.sent, [])

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

    def test_live_send_splits_api_message_boundaries_when_needed(self) -> None:
        chat = FakeChat()
        reply_lines = [
            "First API message, keep it together.",
            "Second API message, keep it together.",
            "Third API message, keep it together.",
            "Fourth API message.",
            "Fifth API message.",
            "Sixth API message.",
        ]
        llm = FakeLLM(reply="\n".join(reply_lines))
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False, max_reply_chars=0),
            llm_client=llm,
        )

        with patch("wechat_ai.bot.time.sleep"):
            bot._on_message(FakeMessage(), chat)

        self.assertEqual(chat.sent, reply_lines)

    def test_live_send_splits_long_sentence_reply_with_delay(self) -> None:
        chat = FakeChat()
        long_reply = (
            "Topic:We help companies automate customer reception and answer common questions."
            "Details:It can collect lead details and sync key needs to sales teammates."
            "Next:Would you like an example based on your industry?"
        )
        llm = FakeLLM(reply=long_reply)
        bot = WeChatAutoReplyBot(
            config=make_config(dry_run=False, require_manual_confirm=False, max_reply_chars=0),
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
            "This system can help companies receive customers, answer common questions, "
            "collect leads, and sync key needs to sales teammates. "
            "If inquiry volume is high, it can classify customers by intent for follow-up. "
            "Want an example?"
        )

        self.assertGreater(len(parts), 1)
        self.assertGreaterEqual(len(parts[-1]), 12)


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
            bot._queue_buffered_message(pending, "chat", chat, FakeMessage("first"))
            bot._queue_buffered_message(pending, "chat", chat, FakeMessage("second"))
            bot._flush_due_message_buffers(pending)
            self.assertEqual(llm.calls, 0)
            bot._flush_due_message_buffers(pending)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, ["combined"])
        self.assertEqual(llm.histories[-1][-1]["content"], "first\nsecond")

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

    def test_unseeded_unread_poll_processes_recent_messages_only(self) -> None:
        wx = FakeWx(
            [
                FakeMessage("old one", sender="alice"),
                FakeMessage("old two", sender="self"),
                FakeMessage("1", sender="alice"),
                FakeMessage("2", sender="alice"),
                FakeMessage("3", sender="alice"),
            ]
        )
        llm = FakeLLM(reply="combined")
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

        bot._poll_chat_once(wx, "chat", seen, pending, initial_recent_count=3)

        self.assertEqual(llm.calls, 0)
        self.assertIn("chat", pending)
        messages = pending["chat"]["messages"]
        self.assertEqual([msg.content for msg in messages], ["1", "2", "3"])
        self.assertEqual([item["content"] for item in bot.histories["chat"]], ["old one", "old two"])

    def test_session_unread_count_reads_session_info(self) -> None:
        session = FakeSession(isnew=True, new_count=2)
        session.info["new_count"] = 3

        self.assertTrue(WeChatAutoReplyBot._session_has_unread(session))
        self.assertEqual(WeChatAutoReplyBot._session_unread_count(session), 3)

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

        self.assertIn("chat", pending)
        self.assertEqual(llm.calls, 0)

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
        bot = WeChatAutoReplyBot(
            config=make_config(image_screenshot_fallback_enabled=True),
            llm_client=llm,
        )

        bot._seed_history_from_messages(
            "chat",
            [
                FakeMessage("09:00", sender="", msg_type="time"),
                FakeMessage("what does your company do", sender="alice"),
                FakeMessage("we build ai customer service", sender="self"),
            ],
        )

        self.assertEqual(
            list(bot.histories["chat"]),
            [
                {"role": "user", "sender": "alice", "content": "what does your company do"},
                {"role": "assistant", "sender": "self", "content": "we build ai customer service"},
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

    def test_session_scan_reconnect_resets_error_pause_state(self) -> None:
        old_wx = FakeWx()
        new_wx = FakeWx()
        bot = WeChatAutoReplyBot(
            config=make_config(),
            llm_client=FakeLLM(),
        )
        bot.paused = True
        bot.consecutive_errors = 5

        with patch.object(bot, "_create_wechat_client", return_value=new_wx):
            recovered_wx, error_count = bot._reconnect_after_scan_errors(old_wx, 3)

        self.assertIs(recovered_wx, new_wx)
        self.assertEqual(error_count, 0)
        self.assertFalse(bot.paused)
        self.assertEqual(bot.consecutive_errors, 0)

    def test_failed_reconnect_uses_backoff_before_next_attempt(self) -> None:
        clock = [0.0]
        old_wx = FakeWx()
        new_wx = FakeWx()
        bot = WeChatAutoReplyBot(
            config=make_config(
                wechat_reconnect_failure_threshold=1,
                wechat_reconnect_base_delay_seconds=5,
                wechat_reconnect_max_delay_seconds=30,
                alert_file="",
                health_file="",
            ),
            llm_client=FakeLLM(),
        )
        bot._reconnect_backoff = ReconnectBackoff(5, 30, clock=lambda: clock[0])

        with patch.object(
            bot,
            "_create_wechat_client",
            side_effect=[RuntimeError("offline"), new_wx],
        ) as create_client:
            first_wx, first_count = bot._reconnect_after_scan_errors(old_wx, 1)
            delayed_wx, delayed_count = bot._reconnect_after_scan_errors(old_wx, first_count)
            clock[0] = 5.0
            recovered_wx, recovered_count = bot._reconnect_after_scan_errors(old_wx, delayed_count)

        self.assertIs(first_wx, old_wx)
        self.assertIs(delayed_wx, old_wx)
        self.assertIs(recovered_wx, new_wx)
        self.assertEqual(recovered_count, 0)
        self.assertEqual(create_client.call_count, 2)

    def test_runtime_check_rejects_unverified_client_version(self) -> None:
        class RuntimeWx(FakeWx):
            version = "4.1.11.17"

            def IsOnline(self) -> bool:
                return True

        bot = WeChatAutoReplyBot(
            config=make_config(wechat_verified_version_prefixes=["4.1.8.107"]),
            llm_client=FakeLLM(),
        )

        with self.assertRaisesRegex(RuntimeError, "client_version_unsupported"):
            bot._assert_wechat_runtime_safe(RuntimeWx())

    def test_runtime_check_accepts_verified_polling_client(self) -> None:
        class RuntimeWx(FakeWx):
            version = "4.1.8.107"

            def IsOnline(self) -> bool:
                return True

        bot = WeChatAutoReplyBot(config=make_config(), llm_client=FakeLLM())

        bot._assert_wechat_runtime_safe(RuntimeWx())

    def test_heartbeat_writes_health_at_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            health_path = Path(temp_dir) / "health.json"
            bot = WeChatAutoReplyBot(
                config=make_config(health_file=str(health_path)),
                llm_client=FakeLLM(),
            )
            with patch("wechat_ai.bot.time.time", side_effect=[100.0, 105.0, 131.0]):
                bot._heartbeat("first")
                first_text = health_path.read_text(encoding="utf-8")
                bot._heartbeat("second")
                second_text = health_path.read_text(encoding="utf-8")
                bot._heartbeat("third")
                third_text = health_path.read_text(encoding="utf-8")

        self.assertIn("first", first_text)
        self.assertEqual(first_text, second_text)
        self.assertIn("third", third_text)

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
            kb_path.write_text("# Working hours\nWeekdays 9:00-18:00.", encoding="utf-8")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    knowledge_file=str(kb_path),
                    require_knowledge_match=True,
                ),
                llm_client=llm,
            )

            bot._on_message(FakeMessage("zzzqqq"), chat)

        self.assertEqual(llm.calls, 0)
        self.assertEqual(chat.sent, [])

    def test_knowledge_context_is_passed_to_llm(self) -> None:
        chat = FakeChat()
        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_path = Path(temp_dir) / "faq.md"
            kb_path.write_text("# Invoice\nInvoice questions require manual confirmation.", encoding="utf-8")
            bot = WeChatAutoReplyBot(
                config=make_config(knowledge_file=str(kb_path)),
                llm_client=llm,
            )

            bot._on_message(FakeMessage("how to get invoice"), chat)

        self.assertEqual(llm.calls, 1)
        self.assertIn("Invoice questions require manual confirmation", llm.knowledge_contexts[0])


    def test_persistent_buffer_completes_after_one_combined_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            llm = FakeLLM(reply="combined")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(db_path),
                ),
                llm_client=llm,
            )
            chat = FakeChat()
            pending: dict[str, dict[str, object]] = {}
            bot._queue_buffered_message(pending, "chat", chat, FakeMessage("first", msg_id="batch-first"))
            bot._queue_buffered_message(pending, "chat", chat, FakeMessage("second", msg_id="batch-second"))
            batch_id = int(pending["chat"]["batch_id"])

            bot._flush_message_buffer(pending, "chat")

            with bot.store._connect() as conn:
                batch_status = conn.execute(
                    "SELECT status FROM message_batches WHERE id = ?", (batch_id,)
                ).fetchone()["status"]

        self.assertEqual(llm.calls, 1)
        self.assertEqual(llm.histories[-1][-1]["content"], "first\nsecond")
        self.assertEqual(chat.sent, ["combined"])
        self.assertEqual(batch_status, "completed")

    def test_continuous_text_batch_recovers_as_one_reply_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            pending: dict[str, dict[str, object]] = {}
            first_bot._queue_buffered_message(
                pending, "chat", FakeChat(), FakeMessage("one", msg_id="restart-one")
            )
            first_bot._queue_buffered_message(
                pending, "chat", FakeChat(), FakeMessage("two", msg_id="restart-two")
            )

            llm = FakeLLM(reply="recovered combined")
            restarted_bot = WeChatAutoReplyBot(config=config, llm_client=llm)
            wx = FakeWx()
            restarted_bot._recover_and_drain_outbox(wx)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(llm.histories[-1][-1]["content"], "one\ntwo")
        self.assertEqual(wx.sent, [("chat", "recovered combined")])

    def test_transcribed_voice_batch_recovers_without_retranscribing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            voice = FakeVoiceMessage("durable transcript")
            voice.id = "restart-voice"
            pending: dict[str, dict[str, object]] = {}
            first_bot._queue_buffered_message(pending, "chat", FakeChat(), voice)
            batch_id = int(pending["chat"]["batch_id"])
            message_id = int(pending["chat"]["message_ids"][0])
            self.assertTrue(first_bot.store.mark_batch_processing(batch_id))
            resolved, unresolved = first_bot._resolve_buffered_messages(
                [voice], message_ids=[message_id], chat_name="chat"
            )

            llm = FakeLLM(reply="voice recovered")
            restarted_bot = WeChatAutoReplyBot(config=config, llm_client=llm)
            wx = FakeWx()
            restarted_bot._recover_and_drain_outbox(wx)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(unresolved, [])
        self.assertEqual(voice.to_text_calls, 1)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(llm.histories[-1][-1]["content"], "durable transcript")
        self.assertEqual(wx.sent, [("chat", "voice recovered")])

    def test_unresolved_image_recovery_blocks_without_guessing_or_ai(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
                image_understanding_enabled=True,
                image_screenshot_fallback_enabled=True,
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            pending: dict[str, dict[str, object]] = {}
            first_bot._queue_buffered_message(
                pending,
                "chat",
                FakeChat(),
                FakeMessage("[image]", msg_type="image", msg_id="unresolved-image"),
            )

            llm = FakeLLM(reply="must not run")
            restarted_bot = WeChatAutoReplyBot(config=config, llm_client=llm)
            wx = FakeWx()
            with patch("wechat_ai.bot.resolve_recent_wechat_image") as resolve_cache:
                with patch("wechat_ai.bot.open_visible_wechat_image_preview") as open_preview:
                    with patch("wechat_ai.bot.capture_foreground_window_image") as capture_window:
                        restarted_bot._recover_and_drain_outbox(wx)
            blocked = restarted_bot.store.list_blocked_batches()

        self.assertEqual(llm.calls, 0)
        self.assertEqual(wx.sent, [])
        self.assertEqual(len(blocked), 1)
        self.assertIn("verified managed media", blocked[0].error)
        resolve_cache.assert_not_called()
        open_preview.assert_not_called()
        capture_window.assert_not_called()

    def test_blocked_batch_messages_are_not_recovered_individually(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            pending: dict[str, dict[str, object]] = {}
            first_bot._queue_buffered_message(
                pending,
                "chat",
                FakeChat(),
                FakeMessage("[image]", msg_type="image", msg_id="blocked-image"),
            )
            batch_id = int(pending["chat"]["batch_id"])
            first_bot.store.mark_batch_blocked(batch_id, "manual media review")

            llm = FakeLLM(reply="must not run")
            restarted_bot = WeChatAutoReplyBot(config=config, llm_client=llm)
            restarted_bot._recover_and_drain_outbox(FakeWx())

        self.assertEqual(llm.calls, 0)

    def test_local_media_is_atomically_copied_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "state.sqlite3"
            source = root / "source.silk"
            payload = b"durable voice bytes\x00\x01"
            source.write_bytes(payload)
            bot = WeChatAutoReplyBot(
                config=make_config(
                    state_db_file=str(db_path),
                    wechat_media_allowed_roots=[str(root)],
                ),
                llm_client=FakeLLM(),
            )
            voice = FakeVoiceMessage("transcript")
            voice.id = "media-copy"
            voice.file_path = str(source)
            pending: dict[str, dict[str, object]] = {}

            bot._queue_buffered_message(pending, "chat", FakeChat(), voice)
            message_id = int(pending["chat"]["message_ids"][0])
            media = bot.store.get_message_media(message_id)
            managed_path = Path(media.managed_path)

            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), payload)
            self.assertEqual(managed_path.read_bytes(), payload)

        self.assertEqual(media.checksum, hashlib.sha256(payload).hexdigest())
        self.assertEqual(media.size_bytes, len(payload))
        self.assertEqual(media.status, "ready")

    def test_media_source_outside_allowed_root_is_persistently_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowed = root / "allowed"
            allowed.mkdir()
            outside = root / "outside.png"
            outside.write_bytes(b"outside")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    state_db_file=str(root / "state.sqlite3"),
                    wechat_media_allowed_roots=[str(allowed)],
                ),
                llm_client=FakeLLM(),
            )
            assert bot.store is not None
            stored, _ = bot.store.record_message("outside-media", "chat", msg_type="image")
            image = FakeMessage("[image]", msg_type="image")
            image.file_path = str(outside)

            bot._persist_message_media(stored.id, image)
            media = bot.store.get_message_media(stored.id)

        self.assertIsNotNone(media)
        self.assertEqual(media.status, "failed")
        self.assertIn("outside_allowed_root", media.error)

    def test_timed_out_voice_blocks_send_until_worker_finishes(self) -> None:
        started = Event()
        release = Event()

        class BlockingVoice(FakeMessage):
            def __init__(self) -> None:
                super().__init__(content="", msg_type="voice")

            def to_text(self) -> str:
                started.set()
                release.wait(1)
                return "late transcript"

        bot = WeChatAutoReplyBot(
            config=make_config(
                voice_to_text_timeout_seconds=0.01,
                wechat_ui_operation_timeout_seconds=0.01,
                alert_file="",
                health_file="",
            ),
            llm_client=FakeLLM(),
        )
        chat = FakeChat()

        self.assertEqual(bot._voice_to_text(BlockingVoice()), "")
        self.assertTrue(started.is_set())
        with self.assertRaises(OperationRejectedError):
            bot._run_wechat_ui("send_reply", lambda: chat.SendMsg("must wait"))
        self.assertEqual(chat.sent, [])

        release.set()
        self.assertTrue(bot._wechat_operation_gate.wait_until_idle(1))
        bot._run_wechat_ui("send_reply", lambda: chat.SendMsg("now safe"))
        self.assertEqual(chat.sent, ["now safe"])

    def test_duplicate_buffered_message_does_not_extend_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            bot = WeChatAutoReplyBot(
                config=make_config(state_db_file=str(db_path), message_buffer_seconds=3),
                llm_client=FakeLLM(),
            )
            pending: dict[str, dict[str, object]] = {}
            message = FakeMessage("same", msg_id="same-buffered-id")
            with patch("wechat_ai.bot.time.time", return_value=100.0):
                bot._queue_buffered_message(pending, "chat", FakeChat(), message)
            original_deadline = bot.store.list_open_batches()[0].deadline_at
            with patch("wechat_ai.bot.time.time", return_value=500.0):
                bot._queue_buffered_message(pending, "chat", FakeChat(), message)
            unchanged_deadline = bot.store.list_open_batches()[0].deadline_at

        self.assertEqual(original_deadline, 103.0)
        self.assertEqual(unchanged_deadline, original_deadline)
        self.assertEqual(len(pending["chat"]["messages"]), 1)

    def test_claimed_batch_and_concurrent_new_message_use_different_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            llm = FakeLLM(reply="first reply")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(db_path),
                ),
                llm_client=llm,
            )
            pending: dict[str, dict[str, object]] = {}
            chat = FakeChat()
            bot._queue_buffered_message(
                pending, "chat", chat, FakeMessage("first", msg_id="claim-first")
            )
            first_batch_id = int(pending["chat"]["batch_id"])
            claimed = Event()
            release_claim = Event()
            thread_errors: list[BaseException] = []
            real_claim = bot.store.mark_batch_processing

            def delayed_claim(batch_id: int) -> bool:
                result = real_claim(batch_id)
                claimed.set()
                release_claim.wait(timeout=2)
                return result

            def flush() -> None:
                try:
                    bot._flush_message_buffer(pending, "chat")
                except BaseException as exc:
                    thread_errors.append(exc)

            def queue_new() -> None:
                try:
                    bot._queue_buffered_message(
                        pending,
                        "chat",
                        chat,
                        FakeMessage("second", msg_id="claim-second"),
                    )
                except BaseException as exc:
                    thread_errors.append(exc)

            with patch.object(bot.store, "mark_batch_processing", side_effect=delayed_claim):
                flush_thread = Thread(target=flush)
                flush_thread.start()
                self.assertTrue(claimed.wait(timeout=2))
                queue_thread = Thread(target=queue_new)
                queue_thread.start()
                release_claim.set()
                flush_thread.join(timeout=3)
                queue_thread.join(timeout=3)

            second_batch_id = int(pending["chat"]["batch_id"])
            second_messages = bot.store.get_batch_messages(second_batch_id)

        self.assertEqual(thread_errors, [])
        self.assertNotEqual(first_batch_id, second_batch_id)
        self.assertEqual([message.content for message in second_messages], ["second"])
        self.assertEqual(llm.calls, 1)
        self.assertEqual(llm.histories[-1][-1]["content"], "first")

    def test_failed_batch_claim_never_calls_ai(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            llm = FakeLLM(reply="must not run")
            bot = WeChatAutoReplyBot(
                config=make_config(state_db_file=str(db_path)),
                llm_client=llm,
            )
            pending: dict[str, dict[str, object]] = {}
            bot._queue_buffered_message(
                pending, "chat", FakeChat(), FakeMessage("first", msg_id="already-claimed")
            )
            batch_id = int(pending["chat"]["batch_id"])
            self.assertTrue(bot.store.mark_batch_processing(batch_id))

            bot._flush_message_buffer(pending, "chat")

        self.assertEqual(llm.calls, 0)
        self.assertNotIn("chat", pending)

    def test_recovered_collecting_batch_waits_for_persisted_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
                message_buffer_seconds=3,
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            pending: dict[str, dict[str, object]] = {}
            with patch("wechat_ai.bot.time.time", return_value=100.0):
                first_bot._queue_buffered_message(
                    pending,
                    "chat",
                    FakeChat(),
                    FakeMessage("wait for me", msg_id="deadline-message"),
                )

            restarted = WeChatAutoReplyBot(config=config, llm_client=FakeLLM(reply="after deadline"))
            with patch("wechat_ai.bot.time.time", return_value=101.0):
                with patch("wechat_ai.bot.time.sleep") as sleep:
                    restarted._recover_and_drain_outbox(FakeWx())

        self.assertIn(2.0, [args[0] for args, _ in sleep.call_args_list])

    def test_outbox_persistence_failure_blocks_batch_instead_of_completing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                ),
                llm_client=FakeLLM(reply="generated but not queued"),
            )
            pending: dict[str, dict[str, object]] = {}
            chat = FakeChat()
            bot._queue_buffered_message(
                pending,
                "chat",
                chat,
                FakeMessage("question", msg_id="outbox-db-failure"),
            )
            assert bot.store is not None

            with patch.object(bot.store, "enqueue_outbox", side_effect=RuntimeError("database unavailable")):
                with self.assertRaisesRegex(RuntimeError, "persist outbox"):
                    bot._flush_message_buffer(pending, "chat")

            blocked = bot.store.list_blocked_batches()
            completed = [batch for batch in bot.store.list_open_batches() if batch.status == "completed"]

        self.assertEqual(chat.sent, [])
        self.assertEqual(len(blocked), 1)
        self.assertIn("batch_handoff_error", blocked[0].error)
        self.assertEqual(completed, [])

    def test_queued_outbox_is_drained_before_recovered_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            assert first_bot.store is not None
            older, _ = first_bot.store.record_message(
                "older-source", "chat", "alice", "text", "older question"
            )
            first_bot.store.mark_messages_processing([older.id])
            first_bot.store.enqueue_outbox(
                [older.id], "chat", "alice", "text", "auto", "older question", "older reply"
            )
            pending: dict[str, dict[str, object]] = {}
            first_bot._queue_buffered_message(
                pending,
                "chat",
                FakeChat(),
                FakeMessage("newer question", msg_id="newer-batch"),
            )

            restarted = WeChatAutoReplyBot(config=config, llm_client=FakeLLM(reply="newer reply"))
            wx = FakeWx()
            with patch("wechat_ai.bot.time.sleep"):
                restarted._recover_and_drain_outbox(wx)

        self.assertEqual(wx.sent, [("chat", "older reply"), ("chat", "newer reply")])

    def test_recovered_batch_already_handed_to_outbox_is_not_generated_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(db_path),
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            pending: dict[str, dict[str, object]] = {}
            first_bot._queue_buffered_message(
                pending,
                "chat",
                FakeChat(),
                FakeMessage("question", msg_id="handed-off-batch"),
            )
            assert first_bot.store is not None
            batch_id = int(pending["chat"]["batch_id"])
            message_ids = [int(value) for value in pending["chat"]["message_ids"]]
            self.assertTrue(first_bot.store.mark_batch_processing(batch_id))
            first_bot.store.mark_messages_processing(message_ids)
            first_bot.store.enqueue_outbox(
                message_ids,
                "chat",
                "alice",
                "text",
                "auto",
                "question",
                "durable reply",
            )

            llm = FakeLLM(reply="must not regenerate")
            restarted = WeChatAutoReplyBot(config=config, llm_client=llm)
            wx = FakeWx()
            restarted._recover_and_drain_outbox(wx)
            assert restarted.store is not None
            open_batches = restarted.store.list_open_batches()
            blocked_batches = restarted.store.list_blocked_batches()

        self.assertEqual(wx.sent, [("chat", "durable reply")])
        self.assertEqual(llm.calls, 0)
        self.assertEqual(open_batches, [])
        self.assertEqual(blocked_batches, [])

    def test_managed_image_recovers_without_scanning_wechat_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "customer.png"
            source.write_bytes(b"stable image bytes")
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(root / "state.sqlite3"),
                image_understanding_enabled=True,
                wechat_media_allowed_roots=[str(root)],
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            image = FakeMessage("[image]", msg_type="image", msg_id="managed-image")
            image.file_path = str(source)
            first_bot._queue_buffered_message({}, "chat", FakeChat(), image)

            llm = FakeLLM(reply="image recovered")
            restarted = WeChatAutoReplyBot(config=config, llm_client=llm)
            wx = FakeWx()
            with patch.object(restarted.vision_client, "is_configured", return_value=True):
                with patch.object(restarted.vision_client, "describe_image", return_value="invoice image"):
                    with patch("wechat_ai.bot.resolve_recent_wechat_image") as resolve_cache:
                        with patch("wechat_ai.bot.time.sleep"):
                            restarted._recover_and_drain_outbox(wx)

        self.assertEqual(llm.calls, 1)
        self.assertIn("invoice image", llm.histories[-1][-1]["content"])
        self.assertEqual(wx.sent, [("chat", "image recovered")])
        resolve_cache.assert_not_called()

    def test_tampered_managed_media_blocks_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "customer.png"
            source.write_bytes(b"original image")
            config = make_config(
                dry_run=False,
                require_manual_confirm=False,
                state_db_file=str(root / "state.sqlite3"),
                image_understanding_enabled=True,
                wechat_media_allowed_roots=[str(root)],
            )
            first_bot = WeChatAutoReplyBot(config=config, llm_client=FakeLLM())
            image = FakeMessage("[image]", msg_type="image", msg_id="tampered-image")
            image.file_path = str(source)
            pending: dict[str, dict[str, object]] = {}
            first_bot._queue_buffered_message(pending, "chat", FakeChat(), image)
            message_id = int(pending["chat"]["message_ids"][0])
            assert first_bot.store is not None
            media = first_bot.store.get_message_media(message_id)
            Path(media.managed_path).write_bytes(b"tampered")

            llm = FakeLLM(reply="must not run")
            restarted = WeChatAutoReplyBot(config=config, llm_client=llm)
            with patch("wechat_ai.bot.time.sleep"):
                restarted._recover_and_drain_outbox(FakeWx())
            blocked = restarted.store.list_blocked_batches() if restarted.store else []

        self.assertEqual(llm.calls, 0)
        self.assertEqual(len(blocked), 1)
        self.assertIn("verified managed media", blocked[0].error)

    def test_listener_mode_uses_persistent_buffer_before_replying(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLLM(reply="listener combined")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                    message_buffer_seconds=60,
                ),
                llm_client=llm,
            )
            chat = FakeChat()

            bot._on_listener_message(
                FakeMessage("listener message", msg_id="listener-buffered"),
                chat,
            )
            timer = bot._listener_buffer_timers["chat"]
            timer.cancel()

            self.assertEqual(llm.calls, 0)
            self.assertEqual(chat.sent, [])
            self.assertEqual(len(bot.store.list_open_batches()), 1)

            bot._flush_listener_buffer("chat")

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, ["listener combined"])
        self.assertNotIn("chat", bot._listener_pending_buffers)

    def test_persistent_flush_loads_entire_batch_not_only_memory_subset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLLM(reply="complete batch")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                ),
                llm_client=llm,
            )
            assert bot.store is not None
            older, batch, _ = bot.store.record_buffered_message(
                "older-startup-message",
                "chat",
                "alice",
                "text",
                "older",
                time.time() + 3,
            )
            bot.store.set_message_resolved_content(older.id, "older")
            pending: dict[str, dict[str, object]] = {}
            bot._queue_buffered_message(
                pending,
                "chat",
                FakeChat(),
                FakeMessage("newer", msg_id="new-during-startup"),
            )

            self.assertEqual(int(pending["chat"]["batch_id"]), batch.id)
            self.assertEqual(len(pending["chat"]["messages"]), 1)
            chat = pending["chat"]["chat"]
            bot._flush_message_buffer(pending, "chat")

        self.assertEqual(llm.calls, 1)
        self.assertIn("older\nnewer", llm.histories[-1][-1]["content"])
        self.assertEqual(chat.sent, ["complete batch"])

    def test_new_listener_message_replaces_stale_memory_buffer_after_batch_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                ),
                llm_client=FakeLLM(),
            )
            pending: dict[str, dict[str, object]] = {}
            bot._queue_buffered_message(
                pending,
                "chat",
                FakeChat(),
                FakeMessage("old", msg_id="old-claimed"),
            )
            old_batch_id = int(pending["chat"]["batch_id"])
            assert bot.store is not None
            self.assertTrue(bot.store.mark_batch_processing(old_batch_id))

            bot._queue_buffered_message(
                pending,
                "chat",
                FakeChat(),
                FakeMessage("new", msg_id="new-after-claim"),
            )
            new_batch_id = int(pending["chat"]["batch_id"])

        self.assertNotEqual(new_batch_id, old_batch_id)
        self.assertEqual(
            [message.content for message in pending["chat"]["messages"]],
            ["new"],
        )

    def test_stale_listener_timer_cannot_remove_latest_timer(self) -> None:
        class CapturedTimer:
            def __init__(self, interval: float, function: object, args: tuple[object, ...]) -> None:
                self.interval = interval
                self.function = function
                self.args = args
                self.cancelled = False

            def start(self) -> None:
                return None

            def cancel(self) -> None:
                self.cancelled = True

        with tempfile.TemporaryDirectory() as temp_dir:
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                    message_buffer_seconds=60,
                ),
                llm_client=FakeLLM(),
            )
            chat = FakeChat()
            with patch("wechat_ai.bot.threading.Timer", CapturedTimer):
                bot._on_listener_message(FakeMessage("one", msg_id="timer-one"), chat)
                old_timer = bot._listener_buffer_timers["chat"]
                bot._on_listener_message(FakeMessage("two", msg_id="timer-two"), chat)
                new_timer = bot._listener_buffer_timers["chat"]
                old_timer.function(*old_timer.args)

        self.assertTrue(old_timer.cancelled)
        self.assertIs(bot._listener_buffer_timers["chat"], new_timer)
        self.assertEqual(len(bot._listener_pending_buffers["chat"]["messages"]), 2)

    def test_listener_keeps_timer_when_resolved_content_write_needs_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLLM(reply="retried")
            bot = WeChatAutoReplyBot(
                config=make_config(
                    dry_run=False,
                    require_manual_confirm=False,
                    state_db_file=str(Path(temp_dir) / "state.sqlite3"),
                    message_buffer_seconds=60,
                ),
                llm_client=llm,
            )
            assert bot.store is not None
            chat = FakeChat()
            with patch.object(
                bot.store,
                "set_message_resolved_content",
                side_effect=RuntimeError("temporary database failure"),
            ):
                bot._on_listener_message(
                    FakeMessage("durable raw text", msg_id="resolved-write-retry"),
                    chat,
                )
            timer = bot._listener_buffer_timers["chat"]
            timer.cancel()

            bot._flush_listener_buffer("chat")

        self.assertEqual(llm.calls, 1)
        self.assertEqual(chat.sent, ["retried"])
        self.assertNotIn("chat", bot._listener_pending_buffers)


if __name__ == "__main__":
    unittest.main()
