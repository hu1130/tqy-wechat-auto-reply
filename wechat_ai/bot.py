from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
import json
import platform
import queue
import re
import threading
import time
from typing import Any

from wechat_ai.config import AppConfig
from wechat_ai.knowledge import KnowledgeBase
from wechat_ai.llm import LLMClient
from wechat_ai.ops import append_alert, append_rotating_line, redact_sensitive_text, write_health_file
from wechat_ai.store import SQLiteStore
from wechat_ai.utils import MessageDeduplicator, append_history


REPLY_PART_INTERVAL_SECONDS = 2.5
REPLY_PART_TARGET_CHARS = 45
REPLY_PART_MIN_TAIL_CHARS = 12
VOICE_MESSAGE_TYPES = {"voice", "audio"}
VOICE_MAX_TRANSCRIPTION_ATTEMPTS = 5
SESSION_QUEUE_MAX_PER_TICK = 1


class WeChatAutoReplyBot:
    def __init__(self, config: AppConfig, llm_client: LLMClient) -> None:
        self.config = config
        self.llm_client = llm_client
        self.histories: dict[str, deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=self.config.max_context_messages * 2)
        )
        self.deduplicator = MessageDeduplicator(ttl_seconds=self.config.dedup_window_seconds)
        self.last_reply_at: dict[str, float] = {}
        self.sent_count = 0
        self.sent_counts_by_chat: dict[str, int] = defaultdict(int)
        self.consecutive_errors = 0
        self.paused = False
        self.store = SQLiteStore(config.state_db_file) if config.state_db_file else None
        self.knowledge_base = KnowledgeBase(config.knowledge_file) if config.knowledge_file else None

    def start(self) -> None:
        wx = self._create_wechat_client()
        chat_names = self.config.wechat_listen_chats or ["文件传输助手"]

        self._log(f"Listening chats: {', '.join(chat_names)}")
        self._log(f"LLM: {self.config.llm_provider} / {self.config.llm_model}")
        if self.config.dry_run:
            self._log("Dry-run mode is ON. Replies will not be sent.")
        else:
            self._log("Live mode is ON. Replies will be sent to WeChat.")
            if self.config.require_manual_confirm:
                self._log(
                    "Manual confirmation is ON. "
                    f"Approval mode: {self.config.approval_mode}."
                )
                if self.config.approval_mode == "queue":
                    self._log(f"Pending replies will be written to {self.config.pending_replies_file}.")
            else:
                self._log("Manual confirmation is OFF.")
        if self.config.reply_cooldown_seconds > 0:
            self._log(f"Reply cooldown: {self.config.reply_cooldown_seconds}s per chat.")
        if self.config.message_buffer_seconds > 0:
            self._log(f"Message buffer: {self.config.message_buffer_seconds}s after the latest customer message.")
        if not self.config.group_chats_enabled:
            self._log("Group chat replies are OFF. Matching group-like sessions will be skipped.")
        self._log(f"Default chat policy: {self.config.default_chat_policy}.")
        if self.config.chat_policies:
            policy_text = ", ".join(f"{chat}={policy}" for chat, policy in self.config.chat_policies.items())
            self._log(f"Chat policies: {policy_text}.")
        if self.config.max_sends_per_run > 0 or self.config.max_sends_per_chat > 0:
            self._log(
                "Send limits: "
                f"per run={self.config.max_sends_per_run or 'unlimited'}, "
                f"per chat={self.config.max_sends_per_chat or 'unlimited'}."
            )
        if self.config.log_file:
            self._log(f"Writing logs to {self.config.log_file}.")
        if self.config.audit_log_file:
            self._log(f"Writing audit logs to {self.config.audit_log_file}.")
        if self.config.alert_file:
            self._log(f"Writing alerts to {self.config.alert_file}.")
        if self.store:
            self._log(f"Persisting runtime state to {self.config.state_db_file}.")
        if self.knowledge_base:
            self._log(f"Knowledge base entries loaded: {len(self.knowledge_base.entries)}.")
        self._write_health("starting", "bot initialized")

        if not self._supports_listener_api(wx):
            self._start_polling(wx, chat_names)
            return

        for chat_name in chat_names:
            try:
                wx.AddListenChat(chat_name, self._on_message)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to listen to chat {chat_name!r}. Make sure the chat exists and is visible in WeChat."
                ) from exc

        self._log("Bot is running. Press Ctrl+C to stop.")
        self._write_health("running", "bot running")
        wx.KeepRunning()

    def _start_polling(self, wx: Any, chat_names: list[str]) -> None:
        if self._is_session_scan_mode(chat_names):
            self._start_session_scan_polling(wx)
            return

        interval_seconds = 1 if len(chat_names) == 1 else 5
        self._log(f"wxauto4 listener API is unavailable. Falling back to polling mode every {interval_seconds}s.")
        seen: dict[str, set[str]] = {chat_name: set() for chat_name in chat_names}
        pending_buffers: dict[str, dict[str, Any]] = {}

        for chat_name in chat_names:
            try:
                wx.ChatWith(chat_name)
                messages = wx.GetAllMessage()
                seen[chat_name].update(self._message_key(msg) for msg in messages)
                self._seed_history_from_messages(chat_name, messages)
                self._log(f"Polling chat {chat_name}; seeded {len(seen[chat_name])} visible messages.")
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to open chat {chat_name!r} for polling. Make sure the chat exists and is visible in WeChat."
                ) from exc

        self._log("Bot is running in polling mode. Press Ctrl+C to stop.")
        self._write_health("running", "bot polling")

        if len(chat_names) == 1:
            chat_name = chat_names[0]
            chat = _PollingChat(wx, chat_name, switch_before_send=False)
            while True:
                try:
                    for msg in wx.GetAllMessage():
                        key = self._message_key(msg)
                        if key in seen[chat_name]:
                            continue
                        seen[chat_name].add(key)
                        if self._message_should_buffer(msg, chat):
                            self._queue_buffered_message(pending_buffers, chat_name, chat, msg)
                        else:
                            self._flush_message_buffer(pending_buffers, chat_name)
                            self._on_message(msg, chat)
                    self._flush_due_message_buffers(pending_buffers)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    self._record_error(f"Polling error for {chat_name}: {exc}", chat_name=chat_name)
                time.sleep(interval_seconds)

        chat_index = 0
        while True:
            chat_name = chat_names[chat_index % len(chat_names)]
            chat_index += 1
            try:
                wx.ChatWith(chat_name)
                chat = _PollingChat(wx, chat_name, switch_before_send=True)
                for msg in wx.GetAllMessage():
                    key = self._message_key(msg)
                    if key in seen[chat_name]:
                        continue
                    seen[chat_name].add(key)
                    if self._message_should_buffer(msg, chat):
                        self._queue_buffered_message(pending_buffers, chat_name, chat, msg)
                    else:
                        self._flush_message_buffer(pending_buffers, chat_name)
                        self._on_message(msg, chat)
                self._flush_due_message_buffers(pending_buffers)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._record_error(f"Polling error for {chat_name}: {exc}", chat_name=chat_name)
            time.sleep(interval_seconds)

    def _start_session_scan_polling(self, wx: Any) -> None:
        interval_seconds = 1
        self._log("Session scan mode is ON. Scanning visible WeChat chats from the left session list.")
        seen: dict[str, set[str]] = {}
        session_signatures: dict[str, str] = {}
        pending_buffers: dict[str, dict[str, Any]] = {}

        for session in self._get_sessions(wx):
            chat_name = self._session_name(session)
            if not chat_name:
                continue
            if self._should_skip_group_chat(chat_name):
                continue
            session_signatures[chat_name] = self._session_signature(session)
            seen.setdefault(chat_name, set())
            self._seed_polling_chat(wx, chat_name, seen)

        self._log("Bot is running in session scan mode. Press Ctrl+C to stop.")
        self._write_health("running", "bot session scan")

        while True:
            try:
                for session in self._get_sessions(wx):
                    chat_name = self._session_name(session)
                    if not chat_name:
                        continue
                    if self._should_skip_group_chat(chat_name):
                        continue
                    signature = self._session_signature(session)
                    is_new_session = chat_name not in seen
                    changed = session_signatures.get(chat_name) != signature
                    has_unread = self._session_has_unread(session)
                    if is_new_session:
                        session_signatures[chat_name] = signature
                        seen.setdefault(chat_name, set())
                        self._seed_polling_chat(wx, chat_name, seen)
                        continue
                    if not changed and not has_unread:
                        continue
                    session_signatures[chat_name] = signature
                    self._poll_chat_once(wx, chat_name, seen, pending_buffers)
                self._flush_due_message_buffers(pending_buffers)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._record_error(f"Session scan polling error: {exc}")
            time.sleep(interval_seconds)

    def _seed_polling_chat(self, wx: Any, chat_name: str, seen: dict[str, set[str]]) -> None:
        try:
            wx.ChatWith(chat_name)
            messages = wx.GetAllMessage()
            startup_message = self._startup_welcome_message(messages)
            if startup_message:
                self._send_startup_welcome_if_needed(wx, chat_name, startup_message)
            seen.setdefault(chat_name, set()).update(self._message_key(msg) for msg in messages)
            self._seed_history_from_messages(chat_name, messages)
            self._log(f"Session scan seeded {chat_name}; {len(seen[chat_name])} visible messages.")
        except Exception as exc:
            self._record_error(f"Failed to seed scanned chat {chat_name}: {exc}", chat_name=chat_name)

    def _startup_welcome_message(self, messages: list[Any]) -> Any | None:
        for msg in reversed(messages):
            sender = str(getattr(msg, "sender", "")).strip()
            msg_type = str(getattr(msg, "type", "")).strip()
            content = str(getattr(msg, "content", "")).strip()
            if not content:
                continue
            if self._is_self_message(msg, sender):
                return None
            if not (self._message_type_is_text(msg_type) or self._message_is_voice_like(msg_type, content)):
                continue
            return msg
        return None

    def _send_startup_welcome_if_needed(self, wx: Any, chat_name: str, msg: Any) -> None:
        if not self._should_send_welcome(chat_name):
            return
        sender = str(getattr(msg, "sender", "")).strip()
        msg_type = str(getattr(msg, "type", "")).strip() or "text"
        content = str(getattr(msg, "content", "")).strip()
        policy = self._chat_policy(chat_name)
        if policy == "ignore":
            return
        if self._is_chat_paused(chat_name):
            return
        if not self._is_allowed_to_reply(chat_name, sender, msg_type, content, policy):
            return
        dry_run_reply = self.config.dry_run or policy == "dry_run"
        if not dry_run_reply and self._send_limit_reached(chat_name, sender, msg_type, content, policy):
            return
        self._log(f"Visible customer message in {chat_name} has no welcome yet; sending welcome before seeding.")
        chat = _PollingChat(wx, chat_name, switch_before_send=True)
        self._send_welcome(chat_name, sender, msg_type, policy, content, chat, dry_run_reply)

    def _poll_chat_once(
        self,
        wx: Any,
        chat_name: str,
        seen: dict[str, set[str]],
        pending_buffers: dict[str, dict[str, Any]],
    ) -> None:
        wx.ChatWith(chat_name)
        chat = _PollingChat(wx, chat_name, switch_before_send=True)
        seen.setdefault(chat_name, set())
        for _ in range(2):
            found_new_message = False
            for msg in wx.GetAllMessage():
                key = self._message_key(msg)
                if key in seen[chat_name]:
                    continue
                found_new_message = True
                seen[chat_name].add(key)
                if self._message_should_buffer(msg, chat):
                    self._queue_buffered_message(pending_buffers, chat_name, chat, msg)
                else:
                    self._flush_message_buffer(pending_buffers, chat_name)
                    self._on_message(msg, chat)
            if not found_new_message:
                break

    def _get_sessions(self, wx: Any) -> list[Any]:
        get_session = getattr(wx, "GetSession", None)
        if not callable(get_session):
            raise RuntimeError("wxauto4 does not expose GetSession, so --chat \"*\" session scan mode is unavailable.")
        sessions = get_session()
        return list(sessions or [])

    @staticmethod
    def _is_session_scan_mode(chat_names: list[str]) -> bool:
        return any(chat_name.strip() == "*" for chat_name in chat_names)

    @staticmethod
    def _session_name(session: Any) -> str:
        for attr_name in ("name", "who", "title", "nickname", "remark"):
            value = getattr(session, attr_name, None)
            if value:
                return str(value).strip()
        info = getattr(session, "info", None)
        if isinstance(info, dict):
            value = info.get("name")
            if value:
                return str(value).strip()
        return ""

    @staticmethod
    def _session_signature(session: Any) -> str:
        info = getattr(session, "info", None)
        if isinstance(info, dict):
            return "|".join(
                str(info.get(key, ""))
                for key in ("name", "time", "content", "isnew", "new_count")
            )
        return "|".join(
            str(getattr(session, attr_name, ""))
            for attr_name in ("name", "time", "content", "isnew", "new_count")
        )

    @staticmethod
    def _session_has_unread(session: Any) -> bool:
        isnew = bool(getattr(session, "isnew", False))
        try:
            new_count = int(getattr(session, "new_count", 0) or 0)
        except (TypeError, ValueError):
            new_count = 0
        info = getattr(session, "info", None)
        if isinstance(info, dict):
            isnew = isnew or bool(info.get("isnew"))
            try:
                new_count = max(new_count, int(info.get("new_count", 0) or 0))
            except (TypeError, ValueError):
                pass
        return isnew or new_count > 0

    def _create_wechat_client(self) -> Any:
        if platform.system() != "Windows":
            raise RuntimeError(
                "Real WeChat auto-reply requires Windows desktop WeChat because wxauto4 only supports Windows. "
                "Run this project on Windows, then execute `python main.py doctor`."
            )
        try:
            from wxauto4 import WeChat
        except ImportError as exc:
            raise RuntimeError(
                "wxauto4 is not installed. Run `pip install -r requirements.txt` on Windows."
            ) from exc
        try:
            client = WeChat(ads=False, resize=False)
            if not self._supports_listener_api(client) and not self._supports_polling_api(client):
                raise RuntimeError(
                    "Installed wxauto4 exposes neither listener methods "
                    "(AddListenChat, KeepRunning) nor polling methods (ChatWith, GetAllMessage, SendMsg)."
                )
            return client
        except Exception as exc:
            raise RuntimeError(
                "Failed to attach to Windows desktop WeChat 4.x with wxauto4. "
                "Open and log in to a wxauto4-compatible Weixin client such as 4.1.8.107, keep the main window "
                "visible, and run this command with the same privilege level as WeChat. "
                f"Original error: {exc}"
            ) from exc

    @staticmethod
    def _supports_listener_api(wx: Any) -> bool:
        return callable(getattr(wx, "AddListenChat", None)) and callable(getattr(wx, "KeepRunning", None))

    @staticmethod
    def _supports_polling_api(wx: Any) -> bool:
        return all(callable(getattr(wx, name, None)) for name in ("ChatWith", "GetAllMessage", "SendMsg"))

    def _message_key(self, msg: Any) -> str:
        msg_id = getattr(msg, "id", None)
        if msg_id:
            return f"id:{msg_id}"
        parts = [
            type(msg).__name__,
            str(getattr(msg, "attr", "")),
            str(getattr(msg, "sender", "")),
            str(getattr(msg, "type", "")),
            str(getattr(msg, "content", "")),
        ]
        return "|".join(parts)

    def _message_should_buffer(self, msg: Any, chat: Any) -> bool:
        if self.config.message_buffer_seconds <= 0:
            return False
        sender = str(getattr(msg, "sender", "")).strip()
        msg_type = str(getattr(msg, "type", "")).strip()
        return bool(
            (self._message_type_is_text(msg_type) or self._message_type_is_voice(msg_type))
            and not self._is_self_message(msg, sender)
        )

    def _queue_buffered_message(
        self,
        pending_buffers: dict[str, dict[str, Any]],
        chat_name: str,
        chat: Any,
        msg: Any,
    ) -> None:
        buffer = pending_buffers.setdefault(
            chat_name,
            {
                "chat": chat,
                "messages": [],
                "last_seen_at": 0.0,
            },
        )
        buffer["chat"] = chat
        buffer["messages"].append(self._normalise_incoming_message(msg))
        buffer["last_seen_at"] = time.time()

    def _flush_due_message_buffers(self, pending_buffers: dict[str, dict[str, Any]]) -> None:
        if self.config.message_buffer_seconds <= 0:
            return
        now = time.time()
        flushed_count = 0
        for chat_name, buffer in list(pending_buffers.items()):
            if now - float(buffer["last_seen_at"]) >= self.config.message_buffer_seconds:
                self._flush_message_buffer(pending_buffers, chat_name)
                flushed_count += 1
                if flushed_count >= SESSION_QUEUE_MAX_PER_TICK:
                    return

    def _flush_message_buffer(self, pending_buffers: dict[str, dict[str, Any]], chat_name: str) -> None:
        buffer = pending_buffers.get(chat_name)
        if not buffer:
            return
        messages = list(buffer.get("messages", []))
        if not messages:
            pending_buffers.pop(chat_name, None)
            return
        resolved_messages, unresolved_voice_messages = self._resolve_buffered_messages(messages)
        if unresolved_voice_messages:
            attempts = int(buffer.get("voice_attempts", 0)) + 1
            if attempts < VOICE_MAX_TRANSCRIPTION_ATTEMPTS:
                buffer["voice_attempts"] = attempts
                buffer["last_seen_at"] = time.time()
                self._log(
                    f"Waiting for voice transcription in {chat_name}: "
                    f"attempt {attempts}/{VOICE_MAX_TRANSCRIPTION_ATTEMPTS}."
                )
                return
            pending_buffers.pop(chat_name, None)
            self._log(f"Skip reply to {chat_name}: voice message could not be transcribed after {attempts} attempts.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=str(getattr(unresolved_voice_messages[-1], "sender", "")).strip(),
                msg_type=str(getattr(unresolved_voice_messages[-1], "type", "")).strip(),
                content=str(getattr(unresolved_voice_messages[-1], "content", "")).strip(),
                reason="voice_to_text_unavailable",
            )
            return
        combined_message = self._combined_buffered_message(resolved_messages)
        self._log(f"Buffered {len(messages)} customer messages for {chat_name}.")
        cooldown_remaining = self._cooldown_remaining(chat_name)
        if cooldown_remaining > 0:
            buffer["messages"] = resolved_messages
            buffer["last_seen_at"] = time.time()
            buffer["voice_attempts"] = 0
            self._log(
                f"Delay buffered reply to {chat_name}: cooldown has "
                f"{cooldown_remaining:.1f}s remaining."
            )
            return
        pending_buffers.pop(chat_name, None)
        self._on_message(combined_message, buffer["chat"])

    def _resolve_buffered_messages(self, messages: list[Any]) -> tuple[list[Any], list[Any]]:
        resolved_messages: list[Any] = []
        unresolved_voice_messages: list[Any] = []
        for msg in messages:
            content = self._message_content(msg)
            sender = str(getattr(msg, "sender", "")).strip()
            msg_type = str(getattr(msg, "type", "")).strip()
            raw_content = str(getattr(msg, "content", "")).strip()
            if self._message_is_voice_like(msg_type, raw_content):
                if content:
                    resolved_messages.append(_BufferedMessage(content=content, sender=sender, msg_type="text"))
                else:
                    unresolved_voice_messages.append(msg)
                continue
            resolved_messages.append(msg)
        return resolved_messages, unresolved_voice_messages

    def _combined_buffered_message(self, messages: list[Any]) -> "_BufferedMessage":
        contents = []
        for msg in messages:
            content = self._message_content(msg)
            if content:
                contents.append(content)
        last_message = messages[-1]
        return _BufferedMessage(
            content="\n".join(contents),
            sender=str(getattr(last_message, "sender", "")).strip(),
            msg_type="text",
        )

    def _normalise_incoming_message(self, msg: Any) -> Any:
        msg_type = str(getattr(msg, "type", "")).strip()
        if not self._message_type_is_voice(msg_type):
            return msg
        content = self._message_content(msg)
        if not content:
            return msg
        sender = str(getattr(msg, "sender", "")).strip()
        return _BufferedMessage(content=content, sender=sender, msg_type="text")

    def _message_content(self, msg: Any) -> str:
        content = str(getattr(msg, "content", "")).strip()
        msg_type = str(getattr(msg, "type", "")).strip()
        if not self._message_is_voice_like(msg_type, content):
            return content
        voice_text = self._voice_to_text(msg)
        if voice_text:
            return voice_text
        return ""

    def _voice_to_text(self, msg: Any) -> str:
        msg_type = str(getattr(msg, "type", "")).strip()
        content = str(getattr(msg, "content", "")).strip()
        if not self._message_is_voice_like(msg_type, content):
            return ""
        if not self.config.voice_to_text_enabled:
            return ""
        to_text = getattr(msg, "to_text", None)
        if not callable(to_text):
            return ""
        timeout_seconds = self.config.voice_to_text_timeout_seconds
        if timeout_seconds <= 0:
            try:
                return str(to_text()).strip()
            except Exception:
                return ""
        result_queue: queue.Queue[str] = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                result_queue.put(str(to_text()).strip())
            except Exception:
                result_queue.put("")

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            return result_queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return ""

    def _on_message(self, msg: Any, chat: Any) -> None:
        if self.paused:
            self._log("Bot is paused after repeated errors. Restart it after fixing the issue.")
            self._audit("skip", reason="bot_paused")
            return

        sender = str(getattr(msg, "sender", "")).strip()
        msg_type = str(getattr(msg, "type", "")).strip()
        chat_name = self._chat_name(chat)
        policy = self._chat_policy(chat_name)
        raw_content = str(getattr(msg, "content", "")).strip()
        content = self._message_content(msg)

        if not content:
            if self._message_is_voice_like(msg_type, raw_content):
                self._log(f"Skip reply to {chat_name}: voice message could not be transcribed quickly.")
                self._audit(
                    "skip",
                    chat_name=chat_name,
                    sender=sender,
                    msg_type=msg_type,
                    policy=policy,
                    content=raw_content,
                    reason="voice_to_text_unavailable",
                )
            return
        if self._is_chat_paused(chat_name):
            self._log(f"Skip reply to {chat_name}: chat is paused.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="chat_paused",
            )
            return
        if self._is_self_message(msg, sender):
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="self_message",
            )
            return
        if not self._is_allowed_message_type(chat_name, sender, msg_type, content, policy):
            return
        if self.deduplicator.is_duplicate(chat_name, sender, content, msg_type):
            self._log(f"Skip duplicate message from {chat_name}: {content}")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="duplicate_message",
            )
            return
        if policy == "ignore":
            self._log(f"Skip reply to {chat_name}: chat policy is ignore.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="policy_ignore",
            )
            return
        if not self._is_allowed_to_reply(chat_name, sender, msg_type, content, policy):
            return
        sensitive_keyword = self._first_match(content, self.config.sensitive_keywords)
        if sensitive_keyword:
            self._log(f"Skip reply to {chat_name}: sensitive keyword matched: {sensitive_keyword}")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason=f"sensitive_keyword:{sensitive_keyword}",
            )
            return

        self._log(f"Received from {chat_name} ({sender or 'unknown'}): {content}")
        history = self.histories[chat_name]
        append_history(history, role="user", sender=sender or chat_name, content=content)

        knowledge_context = self._knowledge_context(chat_name, sender, msg_type, content, policy)
        if knowledge_context is None:
            return

        cooldown_remaining = self._cooldown_remaining(chat_name)
        if cooldown_remaining > 0:
            self._log(f"Skip reply to {chat_name}: cooldown has {cooldown_remaining:.1f}s remaining.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="cooldown",
            )
            return

        dry_run_reply = self.config.dry_run or policy == "dry_run"
        if not dry_run_reply and self._send_limit_reached(chat_name, sender, msg_type, content, policy):
            return

        if self._should_send_welcome(chat_name):
            self._send_welcome(chat_name, sender, msg_type, policy, content, chat, dry_run_reply)
            return

        try:
            reply = self.llm_client.generate_reply(
                chat_name=chat_name,
                history=list(history),
                knowledge_context=knowledge_context,
            ).strip()
        except RuntimeError as exc:
            self._record_error(
                f"LLM error for {chat_name}: {exc}",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
            )
            return

        if not reply:
            self._log(f"LLM returned empty reply for {chat_name}")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="empty_reply",
            )
            return

        reply_parts = self._split_reply_parts(reply)
        for reply_part in reply_parts:
            reply_block_reason = self._reply_block_reason(reply_part)
            if not reply_block_reason:
                continue
            self._log(f"Skip reply to {chat_name}: {reply_block_reason}")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=reply_part,
                reason=reply_block_reason,
            )
            return

        if dry_run_reply:
            append_history(history, role="assistant", sender="assistant", content=reply)
            self._log(f"[dry-run] Reply to {chat_name}: {reply}")
            self._audit(
                "dry_run",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=reply,
                sent=False,
            )
            self._mark_reply_emitted(chat_name, sent=False)
            return
        if self._requires_manual_confirm(policy):
            if self.config.approval_mode == "queue":
                if self._queue_pending_reply(chat_name, sender, msg_type, policy, content, reply):
                    append_history(history, role="assistant", sender="assistant", content=reply)
                    self._mark_reply_emitted(chat_name, sent=False)
                return
            if not self._confirm_send(chat_name, reply):
                self._log(f"Skipped sending reply to {chat_name}: manual confirmation was not granted.")
                self._audit(
                    "skip",
                    chat_name=chat_name,
                    sender=sender,
                    msg_type=msg_type,
                    policy=policy,
                    content=content,
                    reply=reply,
                    reason="manual_confirmation_denied",
                    sent=False,
                )
                return

        try:
            for index, reply_part in enumerate(reply_parts):
                if index > 0:
                    time.sleep(REPLY_PART_INTERVAL_SECONDS)
                chat.SendMsg(reply_part)
        except Exception as exc:
            self._record_error(
                f"Failed to send reply to {chat_name}: {exc}",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=reply,
            )
            return
        append_history(history, role="assistant", sender="assistant", content=reply)
        self._mark_reply_emitted(chat_name, sent=True)
        self._audit(
            "send",
            chat_name=chat_name,
            sender=sender,
            msg_type=msg_type,
            policy=policy,
            content=content,
            reply=reply,
            sent=True,
        )
        self._log(f"Sent reply to {chat_name}: {reply}")

    def _is_allowed_message_type(
        self,
        chat_name: str,
        sender: str,
        msg_type: str,
        content: str,
        policy: str,
    ) -> bool:
        normalized_type = msg_type.lower()
        if normalized_type in set(self.config.text_message_types) or self._message_type_is_voice(msg_type):
            return True
        self._log(f"Skip reply to {chat_name}: message type {msg_type or 'unknown'} is not allowed.")
        self._audit(
            "skip",
            chat_name=chat_name,
            sender=sender,
            msg_type=msg_type,
            policy=policy,
            content=content,
            reason=f"message_type_not_allowed:{msg_type or 'unknown'}",
        )
        return False

    def _message_type_is_text(self, msg_type: str) -> bool:
        return msg_type.lower() in set(self.config.text_message_types)

    def _message_type_is_voice(self, msg_type: str) -> bool:
        return msg_type.lower() in VOICE_MESSAGE_TYPES

    def _should_skip_group_chat(self, chat_name: str) -> bool:
        if self.config.group_chats_enabled:
            return False
        if self._is_explicitly_allowed_chat(chat_name):
            return False
        normalized_name = chat_name.strip().casefold()
        for pattern in self.config.group_chat_patterns:
            if pattern and pattern.strip().casefold() in normalized_name:
                self._log(f"Skip group-like chat {chat_name}: group chat replies are disabled.")
                return True
        return False

    def _is_explicitly_allowed_chat(self, chat_name: str) -> bool:
        allowed_chats = set(self.config.reply_allowed_chats)
        return bool(allowed_chats and "*" not in allowed_chats and chat_name in allowed_chats)

    def _message_is_voice_like(self, msg_type: str, content: str) -> bool:
        return self._message_type_is_voice(msg_type) or self._looks_like_voice_placeholder(content)

    @staticmethod
    def _looks_like_voice_placeholder(content: str) -> bool:
        return bool(
            content
            and (
                ("\u8bed\u97f3" in content and "\u672a\u64ad\u653e" in content)
                or ("\u7487\uE5E2\u7176" in content and "\u93C8\uE041\u6331" in content)
            )
        )

    def _is_allowed_to_reply(
        self,
        chat_name: str,
        sender: str,
        msg_type: str,
        content: str,
        policy: str,
    ) -> bool:
        if not self.config.reply_allowed_chats:
            self._log(f"Skip reply to {chat_name}: REPLY_ALLOWED_CHATS is empty.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="missing_reply_allowed_chats",
            )
            return False
        allowed_chats = set(self.config.reply_allowed_chats)
        if "*" not in allowed_chats and chat_name not in allowed_chats:
            self._log(f"Skip reply to {chat_name}: chat is not in REPLY_ALLOWED_CHATS.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="chat_not_allowed",
            )
            return False
        if chat_name in set(self.config.reply_blocked_chats):
            self._log(f"Skip reply to {chat_name}: chat is in REPLY_BLOCKED_CHATS.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="chat_blocked",
            )
            return False
        allowed_senders = set(self.config.reply_allowed_senders)
        if allowed_senders and "*" not in allowed_senders and sender not in allowed_senders:
            self._log(f"Skip reply to {chat_name}: sender {sender or 'unknown'} is not in REPLY_ALLOWED_SENDERS.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="sender_not_allowed",
            )
            return False
        if sender and sender in set(self.config.reply_blocked_senders):
            self._log(f"Skip reply to {chat_name}: sender {sender} is in REPLY_BLOCKED_SENDERS.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="sender_blocked",
            )
            return False
        return True

    def _should_send_welcome(self, chat_name: str) -> bool:
        if not self.config.welcome_message_enabled or not self.config.welcome_message:
            return False
        if not self.store:
            return False
        return not self.store.has_sent_welcome(chat_name)

    def _send_welcome(
        self,
        chat_name: str,
        sender: str,
        msg_type: str,
        policy: str,
        content: str,
        chat: Any,
        dry_run_reply: bool,
    ) -> None:
        welcome = self.config.welcome_message.strip()
        history = self.histories[chat_name]
        if dry_run_reply:
            append_history(history, role="assistant", sender="assistant", content=welcome)
            if self.store:
                self.store.mark_welcome_sent(chat_name)
            self._log(f"[dry-run] Welcome to {chat_name}: {welcome}")
            self._audit(
                "welcome",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=welcome,
                sent=False,
            )
            self._mark_reply_emitted(chat_name, sent=False)
            return
        try:
            chat.SendMsg(welcome)
        except Exception as exc:
            self._record_error(
                f"Failed to send welcome to {chat_name}: {exc}",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=welcome,
            )
            return
        append_history(history, role="assistant", sender="assistant", content=welcome)
        if self.store:
            self.store.mark_welcome_sent(chat_name)
        self._mark_reply_emitted(chat_name, sent=True)
        self._audit(
            "welcome",
            chat_name=chat_name,
            sender=sender,
            msg_type=msg_type,
            policy=policy,
            content=content,
            reply=welcome,
            sent=True,
        )
        self._log(f"Sent welcome to {chat_name}: {welcome}")

    def _knowledge_context(
        self,
        chat_name: str,
        sender: str,
        msg_type: str,
        content: str,
        policy: str,
    ) -> str | None:
        if not self.knowledge_base or self.config.knowledge_top_k <= 0:
            return ""
        context = self.knowledge_base.build_context(content, limit=self.config.knowledge_top_k)
        if context:
            return context
        if not self.config.require_knowledge_match:
            return ""
        self._log(f"Skip reply to {chat_name}: no matching knowledge base entry.")
        self._audit(
            "skip",
            chat_name=chat_name,
            sender=sender,
            msg_type=msg_type,
            policy=policy,
            content=content,
            reason="knowledge_no_match",
        )
        return None

    def _cooldown_remaining(self, chat_name: str) -> float:
        if self.config.reply_cooldown_seconds <= 0:
            return 0.0
        last_reply_at = self.last_reply_at.get(chat_name, 0.0)
        if self.store:
            last_reply_at = max(last_reply_at, self.store.get_chat_state(chat_name).last_reply_at)
        if last_reply_at is None:
            return 0.0
        elapsed = time.time() - last_reply_at
        return max(0.0, self.config.reply_cooldown_seconds - elapsed)

    def _send_limit_reached(
        self,
        chat_name: str,
        sender: str,
        msg_type: str,
        content: str,
        policy: str,
    ) -> bool:
        if self.config.max_sends_per_run > 0 and self.sent_count >= self.config.max_sends_per_run:
            self._log(f"Skip reply to {chat_name}: MAX_SENDS_PER_RUN reached.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="max_sends_per_run",
            )
            return True
        if (
            self.config.max_sends_per_chat > 0
            and self.sent_counts_by_chat[chat_name] >= self.config.max_sends_per_chat
        ):
            self._log(f"Skip reply to {chat_name}: MAX_SENDS_PER_CHAT reached.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reason="max_sends_per_chat",
            )
            return True
        if self.store and self.config.max_sends_per_day > 0:
            sent_today = self.store.count_sent()
            if sent_today >= self.config.max_sends_per_day:
                self._log(f"Skip reply to {chat_name}: MAX_SENDS_PER_DAY reached.")
                self._audit(
                    "skip",
                    chat_name=chat_name,
                    sender=sender,
                    msg_type=msg_type,
                    policy=policy,
                    content=content,
                    reason="max_sends_per_day",
                )
                return True
        if self.store and self.config.max_sends_per_chat_per_day > 0:
            sent_today_for_chat = self.store.count_sent(chat_name=chat_name)
            if sent_today_for_chat >= self.config.max_sends_per_chat_per_day:
                self._log(f"Skip reply to {chat_name}: MAX_SENDS_PER_CHAT_PER_DAY reached.")
                self._audit(
                    "skip",
                    chat_name=chat_name,
                    sender=sender,
                    msg_type=msg_type,
                    policy=policy,
                    content=content,
                    reason="max_sends_per_chat_per_day",
                )
                return True
        return False

    def _confirm_send(self, chat_name: str, reply: str) -> bool:
        self._log(f"Pending reply to {chat_name}: {reply}")
        try:
            answer = input(f"Type YES to send this reply to {chat_name}: ").strip()
        except EOFError:
            self._log("Manual confirmation prompt is unavailable; refusing to send.")
            return False
        return answer == "YES"

    def _queue_pending_reply(
        self,
        chat_name: str,
        sender: str,
        msg_type: str,
        policy: str,
        content: str,
        reply: str,
    ) -> bool:
        if not self.config.pending_replies_file:
            self._log("Pending reply queue is disabled because PENDING_REPLIES_FILE is empty.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=reply,
                reason="pending_replies_file_empty",
                sent=False,
            )
            return False
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "action": "pending_approval",
            "chat_name": chat_name,
            "sender": sender,
            "msg_type": msg_type,
            "policy": policy,
            "content_preview": self._preview(content),
            "reply": redact_sensitive_text(reply),
            "sent": False,
        }
        try:
            append_rotating_line(
                self.config.pending_replies_file,
                json.dumps(event, ensure_ascii=False),
                max_bytes=self.config.log_max_bytes,
                backup_count=self.config.log_backup_count,
            )
        except OSError as exc:
            self._record_error(
                f"Failed to write pending reply queue {self.config.pending_replies_file}: {exc}",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=reply,
            )
            return False
        self._log(f"Queued pending reply to {chat_name}: {reply}")
        self._audit(
            "pending_approval",
            chat_name=chat_name,
            sender=sender,
            msg_type=msg_type,
            policy=policy,
            content=content,
            reply=reply,
            sent=False,
        )
        return True

    def _mark_reply_emitted(self, chat_name: str, sent: bool) -> None:
        now = time.time()
        self.last_reply_at[chat_name] = now
        if self.store:
            self.store.touch_last_reply(chat_name, now)
        self.consecutive_errors = 0
        if sent:
            self.sent_count += 1
            self.sent_counts_by_chat[chat_name] += 1

    def _record_error(
        self,
        message: str,
        chat_name: str = "",
        sender: str = "",
        msg_type: str = "",
        policy: str = "",
        content: str = "",
        reply: str = "",
    ) -> None:
        self.consecutive_errors += 1
        self._log(message)
        self._audit(
            "error",
            chat_name=chat_name,
            sender=sender,
            msg_type=msg_type,
            policy=policy,
            content=content,
            reply=reply,
            reason=message,
        )
        if (
            self.config.max_errors_before_pause > 0
            and self.consecutive_errors >= self.config.max_errors_before_pause
        ):
            self.paused = True
            if self.store and chat_name:
                self.store.pause_chat(chat_name, reason="consecutive_errors")
            self._alert(
                "Bot paused after "
                f"{self.consecutive_errors} consecutive errors"
                + (f" for {chat_name}" if chat_name else "")
            )
            self._write_health("paused", "consecutive errors")
            self._log(
                "Bot paused after "
                f"{self.consecutive_errors} consecutive errors. Restart it after fixing the issue."
            )

    def _chat_policy(self, chat_name: str) -> str:
        if self.store:
            state_policy = self.store.get_chat_state(chat_name).policy
            if state_policy:
                return state_policy
        return self.config.chat_policies.get(chat_name, self.config.default_chat_policy)

    def _is_chat_paused(self, chat_name: str) -> bool:
        return bool(self.store and self.store.get_chat_state(chat_name).paused)

    def _requires_manual_confirm(self, policy: str) -> bool:
        return self.config.require_manual_confirm or policy == "manual"

    @staticmethod
    def _first_match(text: str, patterns: list[str]) -> str:
        normalized = text.casefold()
        for pattern in patterns:
            if pattern and pattern.casefold() in normalized:
                return pattern
        return ""

    def _reply_block_reason(self, reply: str) -> str:
        if self.config.max_reply_chars > 0 and len(reply) > self.config.max_reply_chars:
            return f"reply_too_long:{len(reply)}>{self.config.max_reply_chars}"
        blocked_pattern = self._first_match(reply, self.config.blocked_reply_patterns)
        if blocked_pattern:
            return f"blocked_reply_pattern:{blocked_pattern}"
        return ""

    def _split_reply_parts(self, reply: str) -> list[str]:
        line_parts = [part.strip() for part in reply.splitlines() if part.strip()]
        raw_parts = line_parts or [reply.strip()]
        parts: list[str] = []
        max_chars = self.config.max_reply_chars if self.config.max_reply_chars > 0 else 90
        target_chars = min(max_chars, REPLY_PART_TARGET_CHARS)

        for raw_part in raw_parts:
            parts.extend(self._split_text_part(raw_part, target_chars, max_chars))
        return parts or [reply.strip()]

    @staticmethod
    def _split_text_part(text: str, target_chars: int, max_chars: int) -> list[str]:
        if len(text) <= target_chars:
            return [text]

        pieces = WeChatAutoReplyBot._semantic_pieces(text)
        if not pieces:
            return [text]

        parts: list[str] = []
        current = ""
        for piece in pieces:
            if not current:
                current = piece
                continue
            if len(current) + len(piece) <= target_chars:
                current += piece
                continue
            if len(current) < REPLY_PART_MIN_TAIL_CHARS and len(current) + len(piece) <= max_chars:
                current += piece
                continue
            parts.append(current)
            current = piece
        if current:
            parts.append(current)
        return WeChatAutoReplyBot._merge_short_tail(parts, max_chars)

    @staticmethod
    def _semantic_pieces(text: str) -> list[str]:
        strong_pieces = [
            piece.strip()
            for piece in re.split(r"(?<=[。！？!?；;])", text)
            if piece.strip()
        ]
        pieces: list[str] = []
        for piece in strong_pieces or [text]:
            if len(piece) <= REPLY_PART_TARGET_CHARS * 1.4:
                pieces.append(piece)
                continue
            pieces.extend(
                sub_piece.strip()
                for sub_piece in re.split(r"(?<=[，,、：:])", piece)
                if sub_piece.strip()
            )
        return pieces

    @staticmethod
    def _merge_short_tail(parts: list[str], max_chars: int) -> list[str]:
        if len(parts) < 2:
            return parts
        tail = parts[-1]
        previous = parts[-2]
        if len(tail) < REPLY_PART_MIN_TAIL_CHARS and len(previous) + len(tail) <= max_chars:
            return parts[:-2] + [previous + tail]
        return parts

    def _seed_history_from_messages(self, chat_name: str, messages: list[Any]) -> None:
        history = self.histories[chat_name]
        for msg in messages[-self.config.max_context_messages * 2 :]:
            content = self._message_content(msg)
            sender = str(getattr(msg, "sender", "")).strip()
            msg_type = str(getattr(msg, "type", "")).strip()
            if not content or not (self._message_type_is_text(msg_type) or self._message_type_is_voice(msg_type)):
                continue
            role = "assistant" if self._is_self_message(msg, sender) else "user"
            append_history(history, role=role, sender=sender or role, content=content)
        if history:
            self._log(f"Seeded {len(history)} context messages for {chat_name}.")

    def _is_self_message(self, msg: Any, sender: str) -> bool:
        for attr_name in ("is_self", "isSelf", "from_self", "fromSelf", "self"):
            attr_value = getattr(msg, attr_name, None)
            if attr_value is True:
                return True
        if sender.strip().casefold() in {"self", "me", "我", "自己"}:
            return True
        return bool(sender and sender in set(self.config.wechat_self_names))

    @staticmethod
    def _chat_name(chat: Any) -> str:
        for attr_name in ("who", "name", "nickname"):
            value = getattr(chat, attr_name, None)
            if value:
                return str(value)
        return str(chat)

    def _log(self, message: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {redact_sensitive_text(message)}"
        print(line, flush=True)
        if not self.config.log_file:
            return
        try:
            append_rotating_line(
                self.config.log_file,
                line,
                max_bytes=self.config.log_max_bytes,
                backup_count=self.config.log_backup_count,
            )
        except OSError as exc:
            print(f"[{now}] Failed to write log file {self.config.log_file}: {exc}", flush=True)

    def _audit(
        self,
        action: str,
        chat_name: str = "",
        sender: str = "",
        msg_type: str = "",
        policy: str = "",
        content: str = "",
        reply: str = "",
        reason: str = "",
        sent: bool | None = None,
    ) -> None:
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "chat_name": chat_name,
            "sender": sender,
            "msg_type": msg_type,
            "policy": policy,
            "reason": reason,
            "sent": sent,
            "content_preview": self._preview(content),
            "reply_preview": self._preview(reply),
        }
        if self.config.audit_log_file:
            try:
                append_rotating_line(
                    self.config.audit_log_file,
                    json.dumps(event, ensure_ascii=False),
                    max_bytes=self.config.log_max_bytes,
                    backup_count=self.config.log_backup_count,
                )
            except OSError as exc:
                self._log(f"Failed to write audit log file {self.config.audit_log_file}: {exc}")
        if self.store:
            try:
                self.store.record_event(event)
            except Exception as exc:
                self._log(f"Failed to write audit event to database {self.config.state_db_file}: {exc}")
        self._write_health("running", f"last_action={action}")

    @staticmethod
    def _preview(text: str, limit: int = 160) -> str:
        collapsed = " ".join(redact_sensitive_text(text).split())
        if len(collapsed) <= limit:
            return collapsed
        return f"{collapsed[:limit]}..."

    def _alert(self, message: str) -> None:
        if not self.config.alert_file:
            return
        try:
            append_alert(
                self.config.alert_file,
                message,
                max_bytes=self.config.log_max_bytes,
                backup_count=self.config.log_backup_count,
            )
        except OSError as exc:
            self._log(f"Failed to write alert file {self.config.alert_file}: {exc}")

    def _write_health(self, status: str, message: str) -> None:
        if not self.config.health_file:
            return
        try:
            write_health_file(
                self.config.health_file,
                {
                    "status": status,
                    "message": message,
                    "paused": self.paused,
                    "sent_count": self.sent_count,
                    "consecutive_errors": self.consecutive_errors,
                },
            )
        except OSError as exc:
            self._log(f"Failed to write health file {self.config.health_file}: {exc}")


class _PollingChat:
    def __init__(self, wx: Any, who: str, switch_before_send: bool = True) -> None:
        self.wx = wx
        self.who = who
        self.switch_before_send = switch_before_send

    def SendMsg(self, message: str) -> None:
        if self.switch_before_send:
            self.wx.ChatWith(self.who)
        self.wx.SendMsg(message)


class _BufferedMessage:
    def __init__(self, content: str, sender: str, msg_type: str) -> None:
        self.content = content
        self.sender = sender
        self.type = msg_type
