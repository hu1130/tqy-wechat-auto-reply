from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import tempfile
import threading
import time
from typing import Any

from wechat_ai.config import AppConfig
from wechat_ai.image_resolver import (
    capture_foreground_window_image,
    open_visible_wechat_image_preview,
    resolve_recent_wechat_image,
)
from wechat_ai.knowledge import KnowledgeBase
from wechat_ai.llm import LLMClient
from wechat_ai.ops import append_alert, append_rotating_line, redact_sensitive_text, write_health_file
from wechat_ai.store import SQLiteStore
from wechat_ai.utils import MessageDeduplicator, append_history
from wechat_ai.vision import VisionClient
from wechat_ai.wechat_runtime import (
    CapabilityProbeResult,
    MediaSourceValidator,
    OperationGate,
    OperationGateError,
    ReconnectBackoff,
    WeChatRuntimeProbeResult,
    WeChatVersionProbeResult,
    detect_wechat_client_version,
)


REPLY_PART_INTERVAL_SECONDS = 2.5
REPLY_PART_TARGET_CHARS = 45
REPLY_PART_MIN_TAIL_CHARS = 12
VOICE_MESSAGE_TYPES = {"voice", "audio"}
IMAGE_MESSAGE_TYPES = {"image", "img", "picture", "photo", "pic", "图片", "图像"}
VOICE_MAX_TRANSCRIPTION_ATTEMPTS = 5
SESSION_QUEUE_MAX_PER_TICK = 1
SESSION_SCAN_RECONNECT_ERRORS = 3
HEALTH_HEARTBEAT_INTERVAL_SECONDS = 30
LLM_ERROR_FALLBACK_REPLY = "系统这会儿有点忙，我先帮您记录一下，稍后再继续回复您。"


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
        self.vision_client = VisionClient(config)
        self.resolved_image_paths: set[str] = set()
        self.last_health_at = 0.0
        self.last_wechat_activate_at = 0.0
        self._listener_baseline_active = False
        self._listener_baseline_seen: dict[str, set[str]] = {}
        self._listener_baseline_lock = threading.RLock()
        self._listener_buffer_lock = threading.RLock()
        self._listener_pending_buffers: dict[str, dict[str, Any]] = {}
        self._listener_buffer_timers: dict[str, threading.Timer] = {}
        self._listener_buffer_generations: dict[str, int] = {}
        self._message_persistence_lock = threading.RLock()
        self._welcome_lock = threading.RLock()
        self._outbox_recovered = False
        self._open_batches_recovered = False
        self._pending_messages_recovered = False
        self._wechat_operation_gate = OperationGate()
        try:
            self._media_source_validator = (
                MediaSourceValidator(config.wechat_media_allowed_roots)
                if config.wechat_media_allowed_roots
                else None
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Invalid WECHAT_MEDIA_ALLOWED_ROOTS: {exc}") from exc
        self._reconnect_backoff = ReconnectBackoff(
            config.wechat_reconnect_base_delay_seconds,
            config.wechat_reconnect_max_delay_seconds,
        )

    def start(self) -> None:
        if not self.config.dry_run and not self.store:
            raise RuntimeError(
                "Live mode requires STATE_DB_FILE so messages and outbound deliveries remain durable."
            )
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
        if self.config.wechat_auto_activate_window_enabled:
            self._log(
                "WeChat window auto-activate is ON "
                f"(interval {self.config.wechat_auto_activate_interval_seconds}s)."
            )
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

        self._begin_listener_baseline()
        try:
            for chat_name in chat_names:
                try:
                    chat = wx.AddListenChat(chat_name, self._on_listener_message)
                    self._seed_listener_chat(wx, chat_name, chat)
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to listen to chat {chat_name!r}. Make sure the chat exists and is visible in WeChat. "
                        f"Original error: {exc}"
                    ) from exc
        finally:
            self._finish_listener_baseline()

        self._recover_and_drain_outbox(wx)
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

        self._recover_and_drain_outbox(wx)
        self._log("Bot is running in polling mode. Press Ctrl+C to stop.")
        self._write_health("running", "bot polling")

        if len(chat_names) == 1:
            chat_name = chat_names[0]
            chat = _PollingChat(
                wx,
                chat_name,
                switch_before_send=False,
                retry_on_error=not bool(self.store),
            )
            poll_error_count = 0
            while True:
                try:
                    for msg in wx.GetAllMessage():
                        key = self._message_key(msg)
                        if key in seen[chat_name]:
                            continue
                        if self._message_should_buffer(msg, chat):
                            self._queue_buffered_message(pending_buffers, chat_name, chat, msg)
                        else:
                            self._flush_message_buffer(pending_buffers, chat_name)
                            self._on_message(msg, chat)
                        seen[chat_name].add(key)
                    self._flush_due_message_buffers(pending_buffers)
                    self._drain_outbox(wx)
                    poll_error_count = 0
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    poll_error_count += 1
                    self._record_error(f"Polling error for {chat_name}: {exc}", chat_name=chat_name)
                    previous_wx = wx
                    wx, poll_error_count = self._reconnect_after_scan_errors(wx, poll_error_count)
                    if wx is not previous_wx:
                        wx.ChatWith(chat_name)
                        chat = _PollingChat(
                            wx,
                            chat_name,
                            switch_before_send=False,
                            retry_on_error=not bool(self.store),
                        )
                time.sleep(interval_seconds)

        chat_index = 0
        poll_error_count = 0
        while True:
            chat_name = chat_names[chat_index % len(chat_names)]
            chat_index += 1
            try:
                wx.ChatWith(chat_name)
                chat = _PollingChat(
                    wx,
                    chat_name,
                    switch_before_send=True,
                    retry_on_error=not bool(self.store),
                )
                for msg in wx.GetAllMessage():
                    key = self._message_key(msg)
                    if key in seen[chat_name]:
                        continue
                    if self._message_should_buffer(msg, chat):
                        self._queue_buffered_message(pending_buffers, chat_name, chat, msg)
                    else:
                        self._flush_message_buffer(pending_buffers, chat_name)
                        self._on_message(msg, chat)
                    seen[chat_name].add(key)
                self._flush_due_message_buffers(pending_buffers)
                self._drain_outbox(wx)
                poll_error_count = 0
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                poll_error_count += 1
                self._record_error(f"Polling error for {chat_name}: {exc}", chat_name=chat_name)
                wx, poll_error_count = self._reconnect_after_scan_errors(wx, poll_error_count)
            time.sleep(interval_seconds)

    def _start_session_scan_polling(self, wx: Any) -> None:
        interval_seconds = 1
        self._log("Session scan mode is ON. Scanning visible WeChat chats from the left session list.")
        seen: dict[str, set[str]] = {}
        seeded_chats: set[str] = set()
        session_signatures: dict[str, str] = {}
        pending_buffers: dict[str, dict[str, Any]] = {}
        indexed_count = 0

        self._maybe_activate_wechat_window(force=True)
        for session in self._get_sessions_with_retry(wx):
            chat_name = self._session_name(session)
            if not chat_name:
                continue
            if self._should_skip_group_chat(chat_name):
                continue
            session_signatures[chat_name] = self._session_signature(session)
            seen.setdefault(chat_name, set())
            if self._seed_polling_chat(wx, chat_name, seen):
                seeded_chats.add(chat_name)
            indexed_count += 1

        self._log(f"Session scan established startup baselines for {len(seeded_chats)} of {indexed_count} visible chats.")

        self._recover_and_drain_outbox(wx)
        self._log("Bot is running in session scan mode. Press Ctrl+C to stop.")
        self._write_health("running", "bot session scan")
        scan_error_count = 0

        while True:
            try:
                self._heartbeat("session scan heartbeat")
                if self._operator_recently_active():
                    time.sleep(interval_seconds)
                    continue
                self._maybe_activate_wechat_window()
                self._drain_outbox(wx)
                sessions = self._get_sessions_with_retry(wx)
                if not sessions:
                    raise RuntimeError("WeChat session list is empty after activation retry.")
                for session in sessions:
                    self._heartbeat("session scan active")
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
                        if self._seed_polling_chat(wx, chat_name, seen):
                            seeded_chats.add(chat_name)
                        continue
                    if chat_name not in seeded_chats:
                        if self._seed_polling_chat(wx, chat_name, seen):
                            seeded_chats.add(chat_name)
                        session_signatures[chat_name] = signature
                        continue
                    if not changed and not has_unread:
                        continue
                    session_signatures[chat_name] = signature
                    self._poll_chat_once(wx, chat_name, seen, pending_buffers)
                self._flush_due_message_buffers(pending_buffers)
                self._drain_outbox(wx)
                scan_error_count = 0
                if not self.paused:
                    self.consecutive_errors = 0
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                scan_error_count += 1
                self._record_error(f"Session scan polling error: {exc}")
                wx, scan_error_count = self._reconnect_after_scan_errors(wx, scan_error_count)
            time.sleep(interval_seconds)

    def _operator_recently_active(self) -> bool:
        guard_seconds = self.config.operator_idle_guard_seconds
        if guard_seconds <= 0 or platform.system().lower() != "windows":
            return False
        idle_seconds = self._windows_idle_seconds()
        if idle_seconds is None:
            return False
        if idle_seconds >= guard_seconds:
            return False
        self._heartbeat("operator active; session scan paused")
        return True

    def _maybe_activate_wechat_window(self, force: bool = False, maximize: bool = False) -> None:
        if not self.config.wechat_auto_activate_window_enabled:
            return
        if platform.system().lower() != "windows":
            return
        now = time.time()
        if (
            not force
            and now - self.last_wechat_activate_at < self.config.wechat_auto_activate_interval_seconds
        ):
            return
        self.last_wechat_activate_at = now
        try:
            import uiautomation as auto  # type: ignore[import-not-found]

            window = auto.WindowControl(searchDepth=1, Name="微信")
            if not window.Exists(0.5):
                window = auto.WindowControl(searchDepth=1, ClassName="mmui::MainWindow")
            if not window.Exists(0.5):
                return
            if hasattr(window, "ShowWindow"):
                window.ShowWindow(3 if maximize else 9)
            if hasattr(window, "SetActive"):
                window.SetActive()
            elif hasattr(window, "SwitchToThisWindow"):
                window.SwitchToThisWindow()
            time.sleep(0.1)
        except Exception as exc:
            self._heartbeat(f"wechat auto activate skipped: {exc}")

    @staticmethod
    def _windows_idle_seconds() -> float | None:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("dwTime", ctypes.c_uint),
            ]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        try:
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return None
            tick_count = ctypes.windll.kernel32.GetTickCount()
        except Exception:
            return None
        elapsed_ms = int(tick_count) - int(info.dwTime)
        if elapsed_ms < 0:
            return None
        return elapsed_ms / 1000.0

    def _reconnect_after_scan_errors(self, wx: Any, scan_error_count: int) -> tuple[Any, int]:
        if scan_error_count < self.config.wechat_reconnect_failure_threshold:
            return wx, scan_error_count
        if not self._reconnect_backoff.try_begin():
            wait_seconds = self._reconnect_backoff.seconds_until_retry()
            self._heartbeat(f"wechat reconnect delayed for {wait_seconds:.1f}s")
            return wx, scan_error_count
        self._log("Session scan had repeated errors; reconnecting to WeChat desktop client.")
        try:
            new_wx = self._create_wechat_client()
        except Exception as reconnect_exc:
            state = self._reconnect_backoff.record_failure()
            self._record_error(f"WeChat reconnect failed: {reconnect_exc}")
            self._write_health(
                "reconnecting",
                f"wechat reconnect failed; retry in {state.delay_seconds:.1f}s",
            )
            return wx, scan_error_count
        self._reconnect_backoff.record_success()
        self.consecutive_errors = 0
        self.paused = False
        self._write_health("running", "wechat client reconnected")
        return new_wx, 0

    def _seed_polling_chat(self, wx: Any, chat_name: str, seen: dict[str, set[str]]) -> bool:
        try:
            self._maybe_activate_wechat_window()
            wx.ChatWith(chat_name)
            messages = wx.GetAllMessage()
            chat_seen = seen.setdefault(chat_name, set())
            unseen_messages = []
            for msg in messages:
                key = self._message_key(msg)
                if key in chat_seen:
                    continue
                chat_seen.add(key)
                unseen_messages.append(msg)
            self._seed_history_from_messages(chat_name, unseen_messages)
            self._log(f"Session scan seeded {chat_name}; {len(seen[chat_name])} visible messages.")
            return True
        except Exception as exc:
            self._record_error(f"Failed to seed scanned chat {chat_name}: {exc}", chat_name=chat_name)
            return False

    def _begin_listener_baseline(self) -> None:
        with self._listener_baseline_lock:
            self._listener_baseline_active = True
            self._listener_baseline_seen = {}

    def _finish_listener_baseline(self) -> None:
        with self._listener_baseline_lock:
            self._listener_baseline_active = False

    def _seed_listener_chat(self, wx: Any, chat_name: str, chat: Any) -> None:
        get_all_messages = getattr(chat, "GetAllMessage", None)
        try:
            if callable(get_all_messages):
                messages = list(get_all_messages() or [])
            elif self._supports_polling_api(wx):
                wx.ChatWith(chat_name)
                messages = list(wx.GetAllMessage() or [])
            else:
                raise RuntimeError(
                    "wxauto4 listener did not expose a readable chat history for startup baseline."
                )
        except Exception as exc:
            raise RuntimeError(f"Failed to establish listener startup baseline for {chat_name}: {exc}") from exc
        new_baseline_messages = self._record_listener_baseline(chat_name, messages)
        if new_baseline_messages:
            self._seed_history_from_messages(chat_name, new_baseline_messages)
        self._log(f"Listener chat {chat_name}; seeded {len(messages)} visible messages.")

    def _record_listener_baseline(self, chat_name: str, messages: list[Any]) -> list[Any]:
        new_messages = []
        with self._listener_baseline_lock:
            chat_seen = self._listener_baseline_seen.setdefault(chat_name, set())
            for msg in messages:
                key = self._message_key(msg)
                if key in chat_seen:
                    continue
                chat_seen.add(key)
                new_messages.append(msg)
        return new_messages

    def _on_listener_message(self, msg: Any, chat: Any) -> None:
        chat_name = self._chat_name(chat)
        key = self._message_key(msg)
        seed_message = False
        with self._listener_baseline_lock:
            chat_seen = self._listener_baseline_seen.setdefault(chat_name, set())
            is_baseline_replay = key in chat_seen
            if self._listener_baseline_active or is_baseline_replay:
                if key not in chat_seen:
                    chat_seen.add(key)
                    seed_message = True
                is_baseline = True
            else:
                is_baseline = False
        if not is_baseline:
            if self._message_should_buffer(msg, chat):
                with self._listener_buffer_lock:
                    queued = self._queue_buffered_message(
                        self._listener_pending_buffers,
                        chat_name,
                        chat,
                        msg,
                    )
                    if queued:
                        self._schedule_listener_buffer_flush(chat_name)
                return
            with self._listener_buffer_lock:
                timer = self._listener_buffer_timers.pop(chat_name, None)
                if timer is not None:
                    timer.cancel()
                self._listener_buffer_generations[chat_name] = (
                    self._listener_buffer_generations.get(chat_name, 0) + 1
                )
                self._flush_message_buffer(self._listener_pending_buffers, chat_name)
            self._on_message(msg, chat)
            return
        if seed_message:
            self._seed_history_from_messages(chat_name, [msg])
        self._log(f"Listener startup baseline ignored an existing message in {chat_name}.")

    def _poll_chat_once(
        self,
        wx: Any,
        chat_name: str,
        seen: dict[str, set[str]],
        pending_buffers: dict[str, dict[str, Any]],
        initial_recent_count: int | None = None,
    ) -> None:
        self._maybe_activate_wechat_window()
        wx.ChatWith(chat_name)
        chat = _PollingChat(
            wx,
            chat_name,
            switch_before_send=True,
            retry_on_error=not bool(self.store),
        )
        seen.setdefault(chat_name, set())
        if initial_recent_count is not None:
            messages = wx.GetAllMessage()
            recent_count = max(1, min(initial_recent_count, len(messages)))
            history_messages = messages[:-recent_count]
            recent_messages = messages[-recent_count:]
            seen[chat_name].update(self._message_key(msg) for msg in history_messages)
            self._seed_history_from_messages(chat_name, history_messages)
            self._log(f"Session scan opened {chat_name}; processing {len(recent_messages)} recent unread messages.")
            for msg in recent_messages:
                key = self._message_key(msg)
                if key in seen[chat_name]:
                    continue
                if self._message_should_buffer(msg, chat):
                    self._queue_buffered_message(pending_buffers, chat_name, chat, msg)
                else:
                    self._flush_message_buffer(pending_buffers, chat_name)
                    self._on_message(msg, chat)
                seen[chat_name].add(key)
            return
        for _ in range(2):
            found_new_message = False
            for msg in wx.GetAllMessage():
                key = self._message_key(msg)
                if key in seen[chat_name]:
                    continue
                found_new_message = True
                if self._message_should_buffer(msg, chat):
                    self._queue_buffered_message(pending_buffers, chat_name, chat, msg)
                else:
                    self._flush_message_buffer(pending_buffers, chat_name)
                    self._on_message(msg, chat)
                seen[chat_name].add(key)
            if not found_new_message:
                break

    def _get_sessions(self, wx: Any) -> list[Any]:
        get_session = getattr(wx, "GetSession", None)
        if not callable(get_session):
            raise RuntimeError("wxauto4 does not expose GetSession, so --chat \"*\" session scan mode is unavailable.")
        sessions = get_session()
        return list(sessions or [])

    def _get_sessions_with_retry(self, wx: Any, attempts: int = 3) -> list[Any]:
        for attempt in range(max(1, attempts)):
            if attempt > 0:
                maximize = attempt >= attempts - 1
                self._maybe_activate_wechat_window(force=True, maximize=maximize)
                time.sleep(0.5)
            sessions = self._get_sessions(wx)
            if sessions:
                return sessions
        self._heartbeat("wechat session list empty after activation retry")
        return []

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
        new_count = WeChatAutoReplyBot._session_unread_count(session)
        info = getattr(session, "info", None)
        if isinstance(info, dict):
            isnew = isnew or bool(info.get("isnew"))
        return isnew or new_count > 0

    @staticmethod
    def _session_unread_count(session: Any) -> int:
        counts: list[int] = []
        for value in (
            getattr(session, "new_count", 0),
            getattr(session, "unread_count", 0),
        ):
            try:
                counts.append(int(value or 0))
            except (TypeError, ValueError):
                pass
        info = getattr(session, "info", None)
        if isinstance(info, dict):
            for key in ("new_count", "unread_count"):
                try:
                    counts.append(int(info.get(key, 0) or 0))
                except (TypeError, ValueError):
                    pass
        return max(counts or [0])

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
            self._assert_wechat_runtime_safe(client)
            return client
        except Exception as exc:
            raise RuntimeError(
                "Failed to attach to Windows desktop WeChat 4.x with wxauto4. "
                "Open and log in to a client version listed in WECHAT_VERIFIED_VERSION_PREFIXES, keep the main "
                "window visible, and run this command with the same privilege level as WeChat. "
                f"Original error: {exc}"
            ) from exc

    def _assert_wechat_runtime_safe(self, client: Any) -> None:
        version = detect_wechat_client_version(client, self.config.wechat_client_version)
        verified = tuple(self.config.wechat_verified_version_prefixes)
        version_supported = bool(version and any(version.startswith(prefix) for prefix in verified))
        online_probe = getattr(client, "IsOnline", None)
        errors: list[str] = []
        if callable(online_probe):
            try:
                online = bool(online_probe())
            except Exception as exc:
                online = False
                errors.append(f"online_probe:{exc}")
        else:
            online = True

        capabilities = (
            CapabilityProbeResult("session_scan", callable(getattr(client, "GetSession", None))),
            CapabilityProbeResult("receive_text", callable(getattr(client, "GetAllMessage", None))),
            CapabilityProbeResult("send_text", callable(getattr(client, "SendMsg", None))),
            CapabilityProbeResult("polling_api", self._supports_polling_api(client)),
            CapabilityProbeResult("listener_api", self._supports_listener_api(client)),
        )
        probe = WeChatRuntimeProbeResult(
            online=online,
            version=WeChatVersionProbeResult(
                client_version=version,
                adapter_name="wxauto4_generic",
                supported=version_supported,
                detail=f"verified prefixes: {', '.join(verified)}",
            ),
            capabilities=capabilities,
            errors=tuple(errors),
        )
        required = ["receive_text", "send_text"]
        if self._is_session_scan_mode(self.config.wechat_listen_chats):
            required.extend(("session_scan", "polling_api"))
        elif self._supports_listener_api(client):
            required.append("listener_api")
        else:
            required.append("polling_api")
        decision = probe.evaluate(required)
        if decision.safe_to_run:
            self._log(f"WeChat runtime verified: client={version}, adapter=wxauto4_generic.")
            return
        self._write_health("incompatible", ",".join(decision.reasons))
        raise RuntimeError(
            "WeChat runtime compatibility check failed: "
            f"version={version or 'unknown'}, reasons={','.join(decision.reasons)}, "
            f"missing={','.join(decision.missing_capabilities) or 'none'}. "
            "Set WECHAT_CLIENT_VERSION only after confirming the installed client version, and add it to "
            "WECHAT_VERIFIED_VERSION_PREFIXES only after a regression test."
        )

    @staticmethod
    def _supports_listener_api(wx: Any) -> bool:
        return callable(getattr(wx, "AddListenChat", None)) and callable(getattr(wx, "KeepRunning", None))

    @staticmethod
    def _supports_polling_api(wx: Any) -> bool:
        return all(callable(getattr(wx, name, None)) for name in ("ChatWith", "GetAllMessage", "SendMsg"))

    def _message_key(self, msg: Any) -> str:
        return self._message_fingerprint(msg, "")

    def _message_fingerprint(self, msg: Any, chat_name: str) -> str:
        for attr_name in ("id", "message_id", "msgid"):
            value = self._stable_scalar(getattr(msg, attr_name, None))
            if value:
                payload = json.dumps(
                    {"chat": chat_name, "id": value},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                return f"wechat:id:{digest}"

        sender = self._stable_scalar(getattr(msg, "sender", None))
        msg_type = self._stable_scalar(getattr(msg, "type", None))
        content = self._stable_scalar(getattr(msg, "content", None))
        for attr_name in ("time", "timestamp"):
            value = self._stable_scalar(getattr(msg, attr_name, None))
            if value:
                return self._fingerprint_hash(
                    {
                        "time": value,
                        "chat": chat_name,
                        "sender": sender,
                        "type": msg_type,
                        "content": content,
                    }
                )
        return self._fingerprint_hash(
            {
                "chat": chat_name,
                "sender": sender,
                "type": msg_type,
                "content": content,
            }
        )

    @staticmethod
    def _stable_scalar(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (str, int, float)):
            return str(value).strip()
        return ""

    @staticmethod
    def _fingerprint_hash(fields: dict[str, str]) -> str:
        payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"wechat:sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    def _persist_incoming_message(
        self,
        msg: Any,
        chat_name: str,
    ) -> tuple[list[int], bool]:
        if not self.store:
            return [], False
        sender = str(getattr(msg, "sender", "")).strip()
        msg_type = str(getattr(msg, "type", "")).strip()
        content = str(getattr(msg, "content", "")).strip()
        fingerprint = self._message_fingerprint(msg, chat_name)
        with self._message_persistence_lock:
            stored_message, created = self.store.record_message(
                fingerprint=fingerprint,
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                content=content,
            )
        return [stored_message.id], not created

    def _mark_messages_processing(self, message_ids: list[int]) -> bool:
        if not self.store or not message_ids:
            return True
        try:
            self.store.mark_messages_processing(message_ids)
            return True
        except Exception as exc:
            self._record_error(f"Failed to mark inbound messages processing: {exc}")
            raise RuntimeError("Failed to persist the processing message state.") from exc

    def _mark_messages_generated(self, message_ids: list[int], reply: str) -> bool:
        if not self.store or not message_ids:
            return True
        try:
            self.store.mark_messages_generated(message_ids, reply)
            return True
        except Exception as exc:
            self._record_error(f"Failed to mark inbound messages generated: {exc}")
            raise RuntimeError("Failed to persist the generated message state.") from exc

    def _mark_messages_failed(self, message_ids: list[int], reason: str) -> None:
        if not self.store or not message_ids:
            return
        try:
            self.store.mark_messages_failed(message_ids, reason)
        except Exception as exc:
            self._record_error(f"Failed to mark inbound messages failed: {exc}")
            raise RuntimeError("Failed to persist the failed message state.") from exc

    def _message_should_buffer(self, msg: Any, chat: Any) -> bool:
        if self.config.message_buffer_seconds <= 0:
            return False
        sender = str(getattr(msg, "sender", "")).strip()
        msg_type = str(getattr(msg, "type", "")).strip()
        allowed_buffer_type = (
            self._message_type_is_text(msg_type)
            or self._message_type_is_voice(msg_type)
            or self._message_is_image_like(msg_type, str(getattr(msg, "content", "")).strip())
        )
        return bool(allowed_buffer_type and not self._is_self_message(msg, sender))

    def _queue_buffered_message(
        self,
        pending_buffers: dict[str, dict[str, Any]],
        chat_name: str,
        chat: Any,
        msg: Any,
    ) -> bool:
        if self.store:
            with self._message_persistence_lock:
                return self._queue_buffered_message_locked(pending_buffers, chat_name, chat, msg)
        return self._queue_buffered_message_locked(pending_buffers, chat_name, chat, msg)

    def _queue_buffered_message_locked(
        self,
        pending_buffers: dict[str, dict[str, Any]],
        chat_name: str,
        chat: Any,
        msg: Any,
    ) -> bool:
        now = time.time()
        deadline_at = now + self.config.message_buffer_seconds
        batch_id: int | None = None
        if self.store:
            sender = str(getattr(msg, "sender", "")).strip()
            msg_type = str(getattr(msg, "type", "")).strip()
            content = str(getattr(msg, "content", "")).strip()
            try:
                stored_message, batch, created = self.store.record_buffered_message(
                    fingerprint=self._message_fingerprint(msg, chat_name),
                    chat_name=chat_name,
                    sender=sender,
                    msg_type=msg_type,
                    content=content,
                    deadline_at=deadline_at,
                )
                message_ids = [stored_message.id]
                batch_id = self._stored_batch_id(batch)
                if not created:
                    self._log(f"Skip persisted duplicate message from {chat_name}.")
                    return False
            except Exception as exc:
                self._record_error(
                    f"Failed to persist buffered batch state for {chat_name}: {exc}",
                    chat_name=chat_name,
                )
                raise RuntimeError(f"Failed to persist buffered batch state for {chat_name}.") from exc
            try:
                if (
                    self._message_type_is_text(msg_type)
                    and not self._message_is_image_like(msg_type, content)
                    and not self._message_is_voice_like(msg_type, content)
                    and content
                ):
                    self.store.set_message_resolved_content(stored_message.id, content)
                if self._message_has_media(msg):
                    self._persist_message_media(stored_message.id, msg)
            except Exception as exc:
                self._record_error(
                    f"Buffered batch {batch_id} for {chat_name} was recorded, but its "
                    f"resolved content or media state needs retry: {exc}",
                    chat_name=chat_name,
                )
        else:
            message_ids = []
        buffer = pending_buffers.get(chat_name)
        if buffer is None or (
            batch_id is not None
            and buffer.get("batch_id") not in (None, batch_id)
        ):
            buffer = {
                "chat": chat,
                "messages": [],
                "message_ids": [],
                "last_seen_at": 0.0,
                "deadline_at": deadline_at,
                "batch_id": batch_id,
            }
            pending_buffers[chat_name] = buffer
        existing_batch_id = buffer.get("batch_id")
        buffer["chat"] = chat
        buffer["messages"].append(msg)
        buffer.setdefault("message_ids", []).extend(message_ids)
        buffer["last_seen_at"] = now
        buffer["deadline_at"] = deadline_at
        buffer["batch_id"] = batch_id if batch_id is not None else existing_batch_id
        return True

    def _schedule_listener_buffer_flush(self, chat_name: str) -> None:
        previous_timer = self._listener_buffer_timers.pop(chat_name, None)
        if previous_timer is not None:
            previous_timer.cancel()
        buffer = self._listener_pending_buffers.get(chat_name)
        if not buffer:
            return
        deadline_at = float(buffer.get("deadline_at", time.time()))
        delay = max(0.05, deadline_at - time.time())
        generation = self._listener_buffer_generations.get(chat_name, 0) + 1
        self._listener_buffer_generations[chat_name] = generation
        timer = threading.Timer(
            delay,
            self._flush_listener_buffer,
            args=(chat_name, generation),
        )
        timer.daemon = True
        self._listener_buffer_timers[chat_name] = timer
        timer.start()

    def _flush_listener_buffer(self, chat_name: str, generation: int | None = None) -> None:
        with self._listener_buffer_lock:
            current_generation = self._listener_buffer_generations.get(chat_name, 0)
            if generation is not None and generation != current_generation:
                return
            buffer = self._listener_pending_buffers.get(chat_name)
            if (
                generation is not None
                and buffer is not None
                and time.time() < float(buffer.get("deadline_at", 0.0))
            ):
                self._schedule_listener_buffer_flush(chat_name)
                return
            self._listener_buffer_timers.pop(chat_name, None)
            try:
                self._flush_message_buffer(self._listener_pending_buffers, chat_name)
            except Exception as exc:
                self._record_error(
                    f"Listener buffer flush failed for {chat_name}: {exc}",
                    chat_name=chat_name,
                )
            if chat_name in self._listener_pending_buffers:
                self._schedule_listener_buffer_flush(chat_name)

    def _flush_due_message_buffers(self, pending_buffers: dict[str, dict[str, Any]]) -> None:
        if self.config.message_buffer_seconds <= 0:
            return
        now = time.time()
        flushed_count = 0
        for chat_name, buffer in list(pending_buffers.items()):
            deadline_at = buffer.get("deadline_at")
            due = (
                now >= float(deadline_at)
                if deadline_at is not None
                else now - float(buffer["last_seen_at"]) >= self.config.message_buffer_seconds
            )
            if due:
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
        message_ids = list(buffer.get("message_ids", []))
        batch_id = buffer.get("batch_id")
        cooldown_remaining = self._cooldown_remaining(chat_name)
        if cooldown_remaining > 0:
            retry_at = time.time() + cooldown_remaining
            self._set_buffer_deadline(buffer, batch_id, retry_at)
            self._log(
                f"Delay buffered reply to {chat_name}: cooldown has "
                f"{cooldown_remaining:.1f}s remaining."
            )
            return
        if self.store and batch_id is not None:
            try:
                with self._message_persistence_lock:
                    claimed = self.store.mark_batch_processing(int(batch_id))
                    if pending_buffers.get(chat_name) is buffer:
                        pending_buffers.pop(chat_name, None)
            except Exception as exc:
                self._record_error(
                    f"Failed to claim buffered batch {batch_id}: {exc}",
                    chat_name=chat_name,
                )
                raise
            if not claimed:
                self._log(f"Skip buffered batch {batch_id}: another executor already claimed it.")
                return
            messages, message_ids = self._durable_batch_inputs(
                int(batch_id),
                messages,
                message_ids,
            )
        try:
            resolved_messages, unresolved_messages = self._resolve_buffered_messages(
                messages,
                message_ids=message_ids,
                chat_name=chat_name,
            )
        except Exception as exc:
            if pending_buffers.get(chat_name) is buffer:
                pending_buffers.pop(chat_name, None)
            self._fail_buffered_batch(batch_id, message_ids, chat_name, f"message_resolution_error:{exc}")
            raise

        if unresolved_messages:
            if self.store and batch_id is not None:
                self._block_buffered_batch(
                    int(batch_id),
                    chat_name,
                    "buffered_media_resolution_uncertain",
                )
                return
            attempts = int(buffer.get("voice_attempts", 0)) + 1
            if attempts < VOICE_MAX_TRANSCRIPTION_ATTEMPTS:
                buffer["voice_attempts"] = attempts
                retry_at = time.time() + self.config.message_buffer_seconds
                self._set_buffer_deadline(buffer, batch_id, retry_at)
                self._log(
                    f"Waiting for buffered media resolution in {chat_name}: "
                    f"attempt {attempts}/{VOICE_MAX_TRANSCRIPTION_ATTEMPTS}."
                )
                return
            pending_buffers.pop(chat_name, None)
            self._log(f"Skip reply to {chat_name}: buffered media could not be resolved after {attempts} attempts.")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=str(getattr(unresolved_messages[-1], "sender", "")).strip(),
                msg_type=str(getattr(unresolved_messages[-1], "type", "")).strip(),
                content=str(getattr(unresolved_messages[-1], "content", "")).strip(),
                reason="buffered_media_unavailable",
            )
            self._fail_buffered_batch(
                batch_id,
                message_ids,
                chat_name,
                "buffered_media_unavailable",
            )
            return
        combined_message = self._combined_buffered_message(resolved_messages)
        self._log(f"Buffered {len(messages)} customer messages for {chat_name}.")
        if not self.store and not self._mark_messages_processing(message_ids):
            return
        if pending_buffers.get(chat_name) is buffer:
            pending_buffers.pop(chat_name, None)
        try:
            self._on_message(combined_message, buffer["chat"], message_ids=message_ids)
            if self.store and batch_id is not None:
                self.store.mark_batch_completed(int(batch_id))
        except Exception as exc:
            if self.store and batch_id is not None:
                self._block_buffered_batch(
                    int(batch_id),
                    chat_name,
                    f"batch_handoff_error:{exc}",
                )
            else:
                self._fail_buffered_batch(
                    batch_id,
                    message_ids,
                    chat_name,
                    f"batch_handoff_error:{exc}",
                )
            raise

    def _durable_batch_inputs(
        self,
        batch_id: int,
        memory_messages: list[Any],
        memory_message_ids: list[int],
    ) -> tuple[list[Any], list[int]]:
        if not self.store:
            return memory_messages, memory_message_ids
        memory_by_id = {
            int(message_id): message
            for message_id, message in zip(memory_message_ids, memory_messages)
        }
        stored_messages = self.store.get_batch_messages(batch_id)
        if not stored_messages:
            raise RuntimeError("Persistent batch has no source messages.")
        durable_messages: list[Any] = []
        durable_ids: list[int] = []
        for stored in stored_messages:
            durable_ids.append(int(stored.id))
            durable_messages.append(
                memory_by_id.get(
                    int(stored.id),
                    _BufferedMessage(
                        content=str(stored.content),
                        sender=str(stored.sender),
                        msg_type=str(stored.msg_type),
                    ),
                )
            )
        return durable_messages, durable_ids

    def _resolve_buffered_messages(
        self,
        messages: list[Any],
        message_ids: list[int] | None = None,
        chat_name: str = "",
    ) -> tuple[list[Any], list[Any]]:
        resolved_messages: list[Any] = []
        unresolved_messages: list[Any] = []
        ids = message_ids or []
        for index, msg in enumerate(messages):
            message_id = ids[index] if index < len(ids) else None
            sender = str(getattr(msg, "sender", "")).strip()
            msg_type = str(getattr(msg, "type", "")).strip()
            raw_content = str(getattr(msg, "content", "")).strip()
            content = self._stored_resolved_content(message_id)
            if not content:
                if self._message_is_image_like(msg_type, raw_content):
                    managed_path = self._validated_managed_media_path(message_id)
                    content = self._recovered_image_content(chat_name, sender, managed_path)
                    if not content and (not self.store or message_id is None):
                        content = self._image_message_content(chat_name, msg)
                else:
                    content = self._message_content(msg)
            if self._message_is_voice_like(msg_type, raw_content) and self._voice_content_is_uncertain(content):
                content = ""
            if not content:
                unresolved_messages.append(msg)
                self._mark_media_failed_if_recorded(message_id, "media_resolution_unavailable")
                continue
            if self.store and message_id is not None:
                self.store.set_message_resolved_content(message_id, content)
                self._mark_media_processed_if_recorded(message_id)
            resolved_messages.append(_BufferedMessage(content=content, sender=sender, msg_type="text"))
        return resolved_messages, unresolved_messages

    @staticmethod
    def _stored_batch_id(batch: Any) -> int:
        value = getattr(batch, "id", batch)
        batch_id = int(value)
        if batch_id <= 0:
            raise RuntimeError("Persistent batch id must be positive.")
        return batch_id

    def _set_buffer_deadline(
        self,
        buffer: dict[str, Any],
        batch_id: Any,
        deadline_at: float,
    ) -> None:
        buffer["last_seen_at"] = time.time()
        buffer["deadline_at"] = deadline_at
        if self.store and batch_id is not None:
            self.store.update_batch_deadline(int(batch_id), deadline_at)

    def _fail_buffered_batch(
        self,
        batch_id: Any,
        message_ids: list[int],
        chat_name: str,
        error: str,
    ) -> None:
        if self.store and batch_id is not None:
            try:
                self.store.mark_batch_failed(int(batch_id), error)
            except Exception as exc:
                self._record_error(
                    f"Failed to mark buffered batch {batch_id} failed: {exc}",
                    chat_name=chat_name,
                )
        else:
            self._mark_messages_failed(message_ids, error)
        self._alert(f"Buffered batch for {chat_name} failed and requires review: {error}")
        self._record_error(f"Buffered batch for {chat_name} failed: {error}", chat_name=chat_name)

    def _block_buffered_batch(self, batch_id: int, chat_name: str, error: str) -> None:
        if not self.store:
            return
        try:
            self.store.mark_batch_blocked(batch_id, error)
        except Exception as exc:
            self._record_error(
                f"Failed to mark buffered batch {batch_id} blocked: {exc}",
                chat_name=chat_name,
            )
            raise
        self._alert(f"Buffered batch #{batch_id} for {chat_name} is blocked: {error}")
        self._record_error(f"Buffered batch #{batch_id} for {chat_name} is blocked: {error}", chat_name=chat_name)

    def _stored_resolved_content(self, message_id: int | None) -> str:
        if not self.store or message_id is None:
            return ""
        stored_message = self.store.get_message(message_id)
        if stored_message is None:
            raise RuntimeError(f"Buffered source message {message_id} no longer exists.")
        return str(getattr(stored_message, "resolved_content", "")).strip()

    @staticmethod
    def _message_media_source(msg: Any) -> str:
        for attr_name in ("file_path", "path", "filepath", "download_path"):
            value = getattr(msg, attr_name, None)
            if isinstance(value, (str, os.PathLike)) and str(value).strip():
                return str(value).strip()
        return ""

    def _message_has_media(self, msg: Any) -> bool:
        msg_type = str(getattr(msg, "type", "")).strip()
        content = str(getattr(msg, "content", "")).strip()
        return self._message_is_voice_like(msg_type, content) or self._message_is_image_like(msg_type, content)

    def _persist_message_media(self, message_id: int, msg: Any) -> None:
        if not self.store:
            return
        source_text = self._message_media_source(msg)
        msg_type = str(getattr(msg, "type", "")).strip()
        content = str(getattr(msg, "content", "")).strip()
        kind = "image" if self._message_is_image_like(msg_type, content) else "voice"
        if not source_text:
            self.store.upsert_message_media(message_id=message_id, kind=kind, status="pending")
            return
        if self._media_source_validator is None:
            error = "WECHAT_MEDIA_ALLOWED_ROOTS is not configured"
            self.store.upsert_message_media(
                message_id=message_id,
                kind=kind,
                source_path=source_text,
                status="failed",
                error=error,
            )
            self.store.mark_message_media_failed(message_id, error)
            return
        validation = self._media_source_validator.validate(source_text, kind)
        if not validation.valid:
            error = f"media_source_rejected:{validation.reason}"
            self.store.upsert_message_media(
                message_id=message_id,
                kind=kind,
                source_path=source_text,
                status="failed",
                error=error,
            )
            self.store.mark_message_media_failed(message_id, error)
            return
        try:
            managed_path, checksum, size_bytes = self._copy_media_to_managed_storage(
                Path(validation.resolved_path)
            )
            self.store.upsert_message_media(
                message_id=message_id,
                kind=kind,
                source_path=source_text,
                managed_path=str(managed_path),
                checksum=checksum,
                size_bytes=size_bytes,
                status="ready",
            )
        except Exception as exc:
            try:
                self.store.upsert_message_media(
                    message_id=message_id,
                    kind=kind,
                    source_path=source_text,
                    managed_path="",
                    checksum="",
                    size_bytes=0,
                    status="failed",
                    error=f"media_copy_failed:{exc}",
                )
                self.store.mark_message_media_failed(message_id, f"media_copy_failed:{exc}")
            except Exception as store_exc:
                raise RuntimeError(
                    f"Media copy failed ({exc}) and its failure state could not be persisted ({store_exc})."
                ) from store_exc

    def _copy_media_to_managed_storage(self, source_path: Path) -> tuple[Path, str, int]:
        if not self.config.state_db_file:
            raise RuntimeError("STATE_DB_FILE is required for managed media storage.")
        source = source_path.expanduser().resolve(strict=True)
        if not source.is_file():
            raise RuntimeError(f"Media source is not a file: {source}")
        media_dir = Path(self.config.state_db_file).expanduser().resolve().parent / "media"
        media_dir.mkdir(parents=True, exist_ok=True)

        file_hash = hashlib.sha256()
        source_size = 0
        file_descriptor, temp_name = tempfile.mkstemp(prefix=".media-", suffix=".tmp", dir=media_dir)
        temp_path = Path(temp_name)
        try:
            with source.open("rb") as source_file, os.fdopen(file_descriptor, "wb") as target_file:
                while True:
                    chunk = source_file.read(1024 * 1024)
                    if not chunk:
                        break
                    file_hash.update(chunk)
                    source_size += len(chunk)
                    target_file.write(chunk)
                target_file.flush()
                os.fsync(target_file.fileno())
            checksum = file_hash.hexdigest()
            suffix = source.suffix.lower() if len(source.suffix) <= 16 else ""
            managed_path = media_dir / f"{checksum}{suffix}"
            os.replace(temp_path, managed_path)
            managed_checksum, managed_size = self._file_metadata(managed_path)
            if managed_checksum != checksum or managed_size != source_size:
                raise RuntimeError("managed media verification failed after atomic replace")
            return managed_path, checksum, source_size
        except Exception:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _file_metadata(path: Path) -> tuple[str, int]:
        file_hash = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as file_handle:
            while True:
                chunk = file_handle.read(1024 * 1024)
                if not chunk:
                    break
                file_hash.update(chunk)
                size_bytes += len(chunk)
        return file_hash.hexdigest(), size_bytes

    def _validated_managed_media_path(self, message_id: int | None) -> str:
        if not self.store or message_id is None:
            return ""
        media = self.store.get_message_media(message_id)
        if media is None:
            return ""
        if str(getattr(media, "status", "")).strip() not in {"ready", "processed"}:
            return ""
        if str(getattr(media, "kind", "")).strip() not in {"image", "voice"}:
            return ""
        managed_path = str(getattr(media, "managed_path", "")).strip()
        expected_checksum = str(getattr(media, "checksum", "")).strip()
        expected_size = int(getattr(media, "size_bytes", 0) or 0)
        if not managed_path:
            return ""
        path = Path(managed_path)
        try:
            if not path.is_file():
                raise RuntimeError("managed media file is missing")
            actual_checksum, actual_size = self._file_metadata(path)
            if (
                not expected_checksum
                or actual_checksum != expected_checksum
                or actual_size != expected_size
            ):
                raise RuntimeError("managed media checksum or size mismatch")
        except Exception as exc:
            self.store.mark_message_media_failed(message_id, str(exc))
            return ""
        return str(path)

    def _mark_media_processed_if_recorded(self, message_id: int | None) -> None:
        if not self.store or message_id is None:
            return
        if self.store.get_message_media(message_id) is not None:
            resolved_content = self._stored_resolved_content(message_id)
            self.store.mark_message_media_processed(message_id, resolved_content)

    def _mark_media_failed_if_recorded(self, message_id: int | None, error: str) -> None:
        if not self.store or message_id is None:
            return
        if self.store.get_message_media(message_id) is not None:
            self.store.mark_message_media_failed(message_id, error)

    def _recovered_image_content(self, chat_name: str, sender: str, image_path: str) -> str:
        if not image_path or not self.vision_client.is_configured():
            return ""
        description = self._describe_image_safely(chat_name, image_path)
        if not description or self._image_description_is_unclear(description):
            return ""
        return (
            f"Customer {sender or chat_name} sent an image. Image recognition result:\n"
            f"{description}\n"
            "Use the conversation context and company knowledge to answer the customer."
        )

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

    def _normalise_incoming_message(self, msg: Any, chat_name: str = "") -> Any:
        msg_type = str(getattr(msg, "type", "")).strip()
        raw_content = str(getattr(msg, "content", "")).strip()
        if self._message_is_image_like(msg_type, raw_content):
            sender = str(getattr(msg, "sender", "")).strip()
            return _BufferedMessage(
                content=self._image_message_content(chat_name, msg),
                sender=sender,
                msg_type="image",
            )
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
        timeout_seconds = float(self.config.voice_to_text_timeout_seconds)
        if timeout_seconds <= 0:
            timeout_seconds = self.config.wechat_ui_operation_timeout_seconds
        try:
            return str(
                self._wechat_operation_gate.run(
                    "voice_to_text",
                    to_text,
                    timeout_seconds=timeout_seconds,
                )
            ).strip()
        except OperationGateError as exc:
            self._alert(f"WeChat voice operation blocked: {exc}")
            self._write_health("blocked", str(exc))
            return ""
        except Exception:
            return ""

    def _image_message_content(self, chat_name: str, msg: Any) -> str:
        if not self.config.image_understanding_enabled:
            return ""
        resolution = resolve_recent_wechat_image(
            seen_at=time.time(),
            roots=self.config.image_cache_roots or None,
            window_seconds=self.config.image_cache_match_window_seconds,
            exclude_paths=self.resolved_image_paths,
        )
        sender = str(getattr(msg, "sender", "")).strip()
        if resolution.found:
            self.resolved_image_paths.add(resolution.path)
            self._log(f"Resolved image for {chat_name}: {resolution.path}")
            file_name = resolution.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            description = self._describe_image_safely(chat_name, resolution.path)
            if self._image_description_is_unclear(description) and self.config.image_screenshot_fallback_enabled:
                self._log(f"Cached image for {chat_name} is unclear; trying visible image screenshot fallback.")
                screenshot_description = self._describe_visible_wechat_image(chat_name)
                if screenshot_description and not self._image_description_is_unclear(screenshot_description):
                    description = screenshot_description
            if description:
                return (
                    f"客户{sender or chat_name}发送了一张图片，图片识别结果如下：\n"
                    f"{description}\n"
                    "请结合聊天上下文和公司资料，像真人客服一样回复客户。"
                )
            return (
                f"客户{sender or chat_name}发送了一张图片，系统已定位到微信缓存文件：{file_name}。"
                "当前还没有接入OCR/视觉模型，不能直接读取图片内容；请结合客户上下文继续沟通，"
                "必要时请客户补充图片里的关键信息。"
            )
        if self.config.image_screenshot_fallback_enabled:
            screenshot_description = self._describe_visible_wechat_image(chat_name)
            if screenshot_description:
                return (
                    f"客户{sender or chat_name}发送了一张图片，微信窗口截图识别结果如下：\n"
                    f"{screenshot_description}\n"
                    "请结合聊天上下文和公司资料，像真人客服一样回复客户。"
                )
        if resolution.status == "ambiguous":
            self._log(
                f"Image for {chat_name} is ambiguous: "
                f"{len(resolution.candidates)} recent cache files."
            )
            return (
                "客户发送了一张图片，但同一时间检测到多张微信缓存图片，系统无法安全确定是哪一张。"
                "请先让客户简单说明图片内容或要咨询的问题。"
            )
        self._log(f"Image for {chat_name} has no cache file match: {resolution.reason}.")
        return (
            "客户发送了一张图片，但当前微信接口没有提供图片路径，缓存中也没有匹配到唯一图片文件。"
            "请先让客户简单说明图片内容或要咨询的问题。"
        )

    def _describe_visible_wechat_image(self, chat_name: str) -> str:
        opened_preview = open_visible_wechat_image_preview()
        if opened_preview:
            self._log(f"Opened visible image preview for {chat_name}; capturing full preview window.")
        screenshot = capture_foreground_window_image(enhance=not opened_preview)
        if not screenshot.found and screenshot.status != "screenshot":
            self._log(f"Image screenshot fallback for {chat_name} failed: {screenshot.reason}.")
            return ""
        self._log(f"Captured WeChat window for image fallback in {chat_name}: {screenshot.path}")
        prompt = (
            "这是当前微信聊天窗口截图。请重点识别左侧客户最近发送的图片内容，"
            "忽略右侧绿色气泡里的机器人回复、聊天列表和输入框。"
            "如果图片里有表格或文字，请尽量提取关键字段；如果看不清，请说明看不清。"
            "只输出事实描述，不要编造。"
        )
        return self._describe_image_safely(chat_name, screenshot.path, prompt=prompt)

    @staticmethod
    def _image_description_is_unclear(description: str) -> bool:
        normalized = description.strip().casefold()
        if not normalized:
            return True
        unclear_markers = (
            "图片关键信息看不清",
            "看不清",
            "无法识别",
            "无法读取",
            "不够清晰",
            "too blurry",
            "not clear",
            "cannot read",
        )
        return any(marker in normalized for marker in unclear_markers)

    def _describe_image_safely(self, chat_name: str, image_path: str, prompt: str = "") -> str:
        if not self.vision_client.is_configured():
            self._log(f"Vision model is not configured; skip image understanding for {chat_name}.")
            return ""
        try:
            description = self.vision_client.describe_image(image_path, prompt=prompt)
        except Exception as exc:
            self._record_error(f"Vision error for {chat_name}: {exc}", chat_name=chat_name)
            return ""
        description = description.strip()
        if description:
            self._log(f"Vision result for {chat_name}: {description[:120]}")
        return description

    def _on_message(self, msg: Any, chat: Any, message_ids: list[int] | None = None) -> None:
        sender = str(getattr(msg, "sender", "")).strip()
        msg_type = str(getattr(msg, "type", "")).strip()
        chat_name = self._chat_name(chat)
        raw_content = str(getattr(msg, "content", "")).strip()
        if message_ids is None:
            try:
                message_ids, duplicate = self._persist_incoming_message(msg, chat_name)
            except Exception as exc:
                self._record_error(
                    f"Failed to persist inbound message for {chat_name}: {exc}",
                    chat_name=chat_name,
                    sender=sender,
                    msg_type=msg_type,
                    content=raw_content,
                )
                raise RuntimeError(f"Failed to persist inbound message for {chat_name}.") from exc
            if duplicate:
                self._log(f"Skip persisted duplicate message from {chat_name}.")
                self._audit(
                    "skip",
                    chat_name=chat_name,
                    sender=sender,
                    msg_type=msg_type,
                    content=raw_content,
                    reason="persistent_duplicate_message",
                )
                return
        if not self._mark_messages_processing(message_ids):
            return
        try:
            policy = self._chat_policy(chat_name)
        except Exception as exc:
            self._mark_messages_failed(message_ids, f"policy_lookup_error:{exc}")
            self._record_error(
                f"Failed to read chat policy for {chat_name}: {exc}",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                content=raw_content,
            )
            return
        if self.paused:
            self._log("Bot is paused after repeated errors. Restart it after fixing the issue.")
            self._audit("skip", chat_name=chat_name, reason="bot_paused")
            self._mark_messages_failed(message_ids, "bot_paused")
            return

        if self._message_is_image_like(msg_type, raw_content):
            content = self._image_message_content(chat_name, msg)
        else:
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
                self._mark_messages_failed(message_ids, "voice_to_text_unavailable")
            else:
                self._log(
                    f"Skip reply to {chat_name}: empty content for message type {msg_type or 'unknown'}."
                )
                self._audit(
                    "skip",
                    chat_name=chat_name,
                    sender=sender,
                    msg_type=msg_type,
                    policy=policy,
                    content=raw_content,
                    reason="empty_message_content",
                )
                self._mark_messages_failed(message_ids, "empty_message_content")
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
            self._mark_messages_failed(message_ids, "chat_paused")
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
            self._mark_messages_failed(message_ids, "self_message")
            return
        if not self._is_allowed_message_type(chat_name, sender, msg_type, content, policy):
            self._mark_messages_failed(message_ids, "message_type_not_allowed")
            return
        if not self.store and self.deduplicator.is_duplicate(chat_name, sender, content, msg_type):
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
            self._mark_messages_failed(message_ids, "duplicate_message")
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
            self._mark_messages_failed(message_ids, "policy_ignore")
            return
        if not self._is_allowed_to_reply(chat_name, sender, msg_type, content, policy):
            self._mark_messages_failed(message_ids, "reply_not_allowed")
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
            self._mark_messages_failed(message_ids, f"sensitive_keyword:{sensitive_keyword}")
            return

        self._log(f"Received from {chat_name} ({sender or 'unknown'}): {content}")
        history = self.histories[chat_name]
        append_history(history, role="user", sender=sender or chat_name, content=content)

        knowledge_context = self._knowledge_context(chat_name, sender, msg_type, content, policy)
        if knowledge_context is None:
            self._mark_messages_failed(message_ids, "knowledge_no_match")
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
            self._mark_messages_failed(message_ids, "cooldown")
            return

        dry_run_reply = self.config.dry_run or policy == "dry_run"
        if not dry_run_reply and self._send_limit_reached(chat_name, sender, msg_type, content, policy):
            self._mark_messages_failed(message_ids, "send_limit_reached")
            return

        if self._try_send_welcome(
            chat_name,
            sender,
            msg_type,
            policy,
            content,
            chat,
            dry_run_reply,
            message_ids,
        ):
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
            if self.store:
                self._mark_messages_failed(message_ids, f"llm_error:{exc}")
                return
            reply = LLM_ERROR_FALLBACK_REPLY

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
            self._mark_messages_failed(message_ids, "empty_reply")
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
            self._mark_messages_failed(message_ids, reply_block_reason)
            return

        if dry_run_reply:
            if not self._mark_messages_generated(message_ids, reply):
                return
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
                if not self._mark_messages_generated(message_ids, reply):
                    return
                if self._queue_pending_reply(chat_name, sender, msg_type, policy, content, reply):
                    append_history(history, role="assistant", sender="assistant", content=reply)
                    self._mark_reply_emitted(chat_name, sent=False)
                else:
                    self._mark_messages_failed(message_ids, "approval_queue_write_failed")
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
                self._mark_messages_failed(message_ids, "manual_confirmation_denied")
                return

        if self.store:
            self._enqueue_and_send_outbox(
                message_ids=message_ids,
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=reply,
                kind="reply",
                chat=chat,
            )
            return

        try:
            self._send_reply_parts(chat, reply)
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

    def _send_reply_parts(self, chat: Any, reply: str) -> None:
        for index, reply_part in enumerate(self._split_reply_parts(reply)):
            if index > 0:
                time.sleep(REPLY_PART_INTERVAL_SECONDS)
            self._run_wechat_ui("send_reply", lambda part=reply_part: chat.SendMsg(part))

    def _run_wechat_ui(self, operation_name: str, operation: Any) -> Any:
        try:
            return self._wechat_operation_gate.run(
                operation_name,
                operation,
                timeout_seconds=self.config.wechat_ui_operation_timeout_seconds,
            )
        except OperationGateError as exc:
            self._alert(f"WeChat UI operation blocked: {exc}")
            self._write_health("blocked", str(exc))
            raise

    def _enqueue_and_send_outbox(
        self,
        message_ids: list[int],
        chat_name: str,
        sender: str,
        msg_type: str,
        policy: str,
        content: str,
        reply: str,
        kind: str,
        chat: Any,
    ) -> bool:
        if not self.store:
            return False
        try:
            job = self.store.enqueue_outbox(
                message_ids=message_ids,
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=reply,
                kind=kind,
            )
            claimed_job = self.store.claim_outbox(job.id)
        except Exception as exc:
            self._record_error(
                f"Failed to enqueue or claim outbox work for {chat_name}: {exc}",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=reply,
            )
            raise RuntimeError(f"Failed to persist outbox work for {chat_name}.") from exc
        if claimed_job is None:
            if job.status == "uncertain":
                self._alert(f"Outbox #{job.id} for {chat_name} is uncertain and will not be retried.")
            return False
        return self._deliver_outbox_job(claimed_job, chat)

    def _deliver_outbox_job(self, job: Any, chat: Any) -> bool:
        if not self.store:
            return False
        reply_parts = self._split_reply_parts(job.reply)
        sent_parts = int(getattr(job, "sent_parts", 0) or 0)
        if sent_parts < 0 or sent_parts > len(reply_parts):
            self.store.mark_outbox_uncertain(job.id, "Persisted outbox part progress is invalid.")
            return False
        try:
            for index in range(sent_parts, len(reply_parts)):
                if index > sent_parts:
                    time.sleep(REPLY_PART_INTERVAL_SECONDS)
                self._run_wechat_ui(
                    "send_outbox",
                    lambda part=reply_parts[index]: chat.SendMsg(part),
                )
                self.store.mark_outbox_part_sent(job.id, index)
        except Exception as exc:
            self._handle_uncertain_outbox(job, str(exc))
            return False

        try:
            self.store.mark_outbox_sent(job.id)
        except Exception as exc:
            error = f"Message send returned successfully, but sent status could not be persisted: {exc}"
            try:
                self.store.mark_outbox_uncertain(job.id, error)
            except Exception:
                pass
            self._alert(f"Outbox #{job.id} delivery to {job.chat_name} is uncertain: {error}")
            self._record_error(error, chat_name=job.chat_name, reply=job.reply)
            return False

        try:
            append_history(self.histories[job.chat_name], role="assistant", sender="assistant", content=job.reply)
            if job.kind == "welcome":
                self.store.mark_welcome_sent(job.chat_name)
            self._mark_reply_emitted(job.chat_name, sent=True)
            action = "welcome" if job.kind == "welcome" else "send"
            self._audit(
                action,
                chat_name=job.chat_name,
                sender=job.sender,
                msg_type=job.msg_type,
                policy=job.policy,
                content=job.content_preview,
                reply=job.reply,
                sent=True,
            )
        except Exception as exc:
            self._log(f"Outbox #{job.id} was sent, but post-send bookkeeping failed: {exc}")
            self._alert(f"Outbox #{job.id} was sent, but post-send bookkeeping requires review: {exc}")
        self._log(f"Sent {job.kind} to {job.chat_name}: {job.reply}")
        return True

    def _handle_uncertain_outbox(self, job: Any, error: str) -> None:
        retry_scheduled = False
        try:
            if self.config.outbox_uncertain_retry_enabled:
                retry_delay = self.config.outbox_uncertain_retry_delay_seconds * (
                    2 ** max(0, int(getattr(job, "attempts", 1) or 1) - 1)
                )
                retry_scheduled = self.store.schedule_outbox_retry(
                    job.id,
                    error,
                    max_attempts=self.config.outbox_uncertain_max_attempts,
                    next_attempt_at=time.time() + retry_delay,
                )
            else:
                self.store.mark_outbox_uncertain(job.id, error)
        except Exception as store_exc:
            error = f"{error}; failed to persist uncertain status: {store_exc}"
        if retry_scheduled:
            message = (
                f"Outbox #{job.id} delivery to {job.chat_name} is uncertain; "
                f"retry {int(getattr(job, 'attempts', 0)) + 1}/"
                f"{self.config.outbox_uncertain_max_attempts} was scheduled: {error}"
            )
        else:
            message = f"Outbox #{job.id} delivery to {job.chat_name} is uncertain: {error}"
        self._alert(message)
        self._record_error(
            message,
            chat_name=job.chat_name,
            sender=job.sender,
            msg_type=job.msg_type,
            policy=job.policy,
            content=job.content_preview,
            reply=job.reply,
        )

    def _recover_and_drain_outbox(self, wx: Any) -> None:
        if not self.store:
            return
        if not self._outbox_recovered:
            try:
                retry_at = time.time() + self.config.outbox_uncertain_retry_delay_seconds
                recovered_count = self.store.recover_interrupted_outbox(
                    retry_enabled=self.config.outbox_uncertain_retry_enabled,
                    max_attempts=self.config.outbox_uncertain_max_attempts,
                    next_attempt_at=retry_at,
                )
                requeued_count = (
                    self.store.requeue_uncertain_outbox(
                        max_attempts=self.config.outbox_uncertain_max_attempts,
                        next_attempt_at=retry_at,
                    )
                    if self.config.outbox_uncertain_retry_enabled
                    else 0
                )
                uncertain_jobs = self.store.list_outbox(status="uncertain", limit=1000)
            except Exception as exc:
                raise RuntimeError(f"Failed to recover interrupted outbox work: {exc}") from exc
            self._outbox_recovered = True
            if recovered_count:
                self._log(f"Recovered {recovered_count} interrupted outbox jobs.")
            if requeued_count:
                self._log(f"Scheduled {requeued_count} uncertain outbox jobs for bounded retry.")
            if uncertain_jobs:
                self._alert(
                    f"{len(uncertain_jobs)} outbox deliveries are uncertain and require manual review; none were retried."
                )
        self._drain_outbox(wx)
        self._recover_open_batches(wx)
        self._recover_pending_messages(wx)

    def _recover_open_batches(self, wx: Any) -> None:
        if not self.store or self.config.dry_run or self._open_batches_recovered:
            return
        try:
            open_batches = self.store.list_open_batches(limit=None)
            blocked_batches = self.store.list_blocked_batches()
        except Exception as exc:
            raise RuntimeError(f"Failed to load persistent message batches: {exc}") from exc
        if blocked_batches:
            self._alert(
                f"{len(blocked_batches)} buffered message batches are blocked and require manual review."
            )
        if open_batches:
            self._log(f"Recovering {len(open_batches)} persistent message batches.")
        open_batches.sort(
            key=lambda batch: (
                str(getattr(batch, "status", "")) != "processing",
                float(getattr(batch, "deadline_at", 0.0) or 0.0),
                self._stored_batch_id(batch),
            )
        )
        for batch in open_batches:
            batch_id = self._stored_batch_id(batch)
            chat_name = str(getattr(batch, "chat_name", "")).strip()
            messages: list[Any] = []
            try:
                if str(getattr(batch, "status", "")) == "collecting":
                    claimed = False
                    while True:
                        current_batch = self.store.get_message_batch(batch_id)
                        if current_batch is None or current_batch.status != "collecting":
                            self._log(
                                f"Skip recovered batch {batch_id}: another executor already claimed it."
                            )
                            break
                        target_deadline = float(current_batch.deadline_at or 0.0)
                        remaining_seconds = max(0.0, target_deadline - time.time())
                        if remaining_seconds > 0:
                            self._log(
                                f"Waiting {remaining_seconds:.1f}s for persistent batch "
                                f"{batch_id} to reach its flush deadline."
                            )
                            time.sleep(remaining_seconds)
                        claimed = self.store.mark_batch_processing(
                            batch_id,
                            ready_at=max(time.time(), target_deadline),
                        )
                        if claimed:
                            break
                        refreshed = self.store.get_message_batch(batch_id)
                        if refreshed is None or refreshed.status != "collecting":
                            break
                    if not claimed:
                        continue
                messages = sorted(
                    self.store.get_batch_messages(batch_id),
                    key=lambda message: (
                        int(getattr(message, "batch_position", 0) or 0),
                        int(message.id),
                    ),
                )
                if not messages:
                    raise RuntimeError("persistent batch has no source messages")
                message_statuses = {str(message.status).strip() for message in messages}
                processing_statuses = {"received", "processing"}
                handed_off_statuses = {"generated", "sending", "sent", "failed"}
                if message_statuses <= handed_off_statuses:
                    self.store.mark_batch_completed(batch_id)
                    continue
                if not message_statuses <= processing_statuses:
                    self._block_buffered_batch(
                        batch_id,
                        chat_name or "unknown chat",
                        "batch contains mixed processing and delivery states: "
                        + ",".join(sorted(message_statuses)),
                    )
                    continue
                resolved_messages: list[Any] = []
                unresolved_ids: list[int] = []
                for message in messages:
                    content = str(getattr(message, "resolved_content", "")).strip()
                    msg_type = str(message.msg_type).strip()
                    raw_content = str(message.content).strip()
                    is_image = self._message_is_image_like(msg_type, raw_content)
                    is_voice = self._message_is_voice_like(msg_type, raw_content)
                    if (
                        not content
                        and self._message_type_is_text(msg_type)
                        and not is_image
                        and not is_voice
                        and raw_content
                    ):
                        content = raw_content
                        self.store.set_message_resolved_content(message.id, content)
                    if not content and is_image:
                        managed_path = self._validated_managed_media_path(message.id)
                        content = self._recovered_image_content(
                            chat_name,
                            str(message.sender).strip(),
                            managed_path,
                        )
                        if content:
                            self.store.set_message_resolved_content(message.id, content)
                            self._mark_media_processed_if_recorded(message.id)
                    if not content:
                        unresolved_ids.append(message.id)
                        continue
                    resolved_messages.append(
                        _BufferedMessage(
                            content=content,
                            sender=str(message.sender).strip(),
                            msg_type="text",
                        )
                    )
                message_ids = [message.id for message in messages]
                if unresolved_ids:
                    self._block_buffered_batch(
                        batch_id,
                        chat_name or "unknown chat",
                        "messages lack durable resolved content or verified managed media: "
                        + ",".join(str(message_id) for message_id in unresolved_ids),
                    )
                    continue
                combined_message = self._combined_buffered_message(resolved_messages)
                chat = _PollingChat(
                    wx,
                    chat_name,
                    switch_before_send=True,
                    retry_on_error=False,
                )
                self._on_message(combined_message, chat, message_ids=message_ids)
                self.store.mark_batch_completed(batch_id)
            except Exception as exc:
                message_ids = [message.id for message in messages]
                self._block_buffered_batch(
                    batch_id,
                    chat_name or "unknown chat",
                    f"batch_recovery_error:{exc}",
                )
        self._open_batches_recovered = True

    def _recover_pending_messages(self, wx: Any) -> None:
        if (
            not self.store
            or self.config.dry_run
            or self._pending_messages_recovered
        ):
            return
        try:
            pending_messages = sorted(
                [
                    *self.store.list_messages(status="received", limit=10000),
                    *self.store.list_messages(status="processing", limit=10000),
                ],
                key=lambda message: message.id,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to load interrupted inbound messages: {exc}") from exc
        pending_messages = [
            message for message in pending_messages if getattr(message, "batch_id", None) is None
        ]
        if pending_messages:
            self._log(f"Recovering {len(pending_messages)} interrupted inbound messages.")
        for message in pending_messages:
            chat = _PollingChat(
                wx,
                message.chat_name,
                switch_before_send=True,
                retry_on_error=False,
            )
            recovered_message = _BufferedMessage(
                content=message.content,
                sender=message.sender,
                msg_type=message.msg_type,
            )
            self._on_message(recovered_message, chat, message_ids=[message.id])
        self._pending_messages_recovered = True

    def _drain_outbox(self, wx: Any) -> None:
        if not self.store or self.config.dry_run:
            return
        while True:
            ready_at = time.time()
            queued_jobs = self.store.list_outbox(status="queued", limit=20, ready_at=ready_at)
            if not queued_jobs:
                return
            claimed_count = 0
            for job in queued_jobs:
                claimed_job = self.store.claim_outbox(job.id, ready_at=ready_at)
                if claimed_job is None:
                    continue
                claimed_count += 1
                chat = _PollingChat(
                    wx,
                    claimed_job.chat_name,
                    switch_before_send=True,
                    retry_on_error=False,
                )
                self._deliver_outbox_job(claimed_job, chat)
            if claimed_count == 0:
                return

    def _is_allowed_message_type(
        self,
        chat_name: str,
        sender: str,
        msg_type: str,
        content: str,
        policy: str,
    ) -> bool:
        normalized_type = msg_type.lower()
        if (
            normalized_type in set(self.config.text_message_types)
            or self._message_type_is_voice(msg_type)
            or self._message_is_image_like(msg_type, content)
        ):
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

    def _message_type_is_image(self, msg_type: str) -> bool:
        return msg_type.lower() in IMAGE_MESSAGE_TYPES

    def _message_is_image_like(self, msg_type: str, content: str) -> bool:
        if self._message_type_is_image(msg_type):
            return True
        normalized_content = content.strip().casefold()
        return normalized_content in {"[图片]", "图片", "[image]", "image", "[photo]", "photo"}

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

    @classmethod
    def _voice_content_is_uncertain(cls, content: str) -> bool:
        normalized = content.strip().casefold()
        return cls._looks_like_voice_placeholder(content) or normalized in {
            "voice",
            "[voice]",
            "audio",
            "[audio]",
            "\u8bed\u97f3",
            "[\u8bed\u97f3]",
        }

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
        if self.store.has_sent_welcome(chat_name):
            return False
        for status in ("queued", "sending", "sent", "uncertain"):
            if any(
                job.kind == "welcome" and job.chat_name == chat_name
                for job in self.store.list_outbox(status=status, limit=1000)
            ):
                return False
        return True

    def _try_send_welcome(
        self,
        chat_name: str,
        sender: str,
        msg_type: str,
        policy: str,
        content: str,
        chat: Any,
        dry_run_reply: bool,
        message_ids: list[int],
    ) -> bool:
        with self._welcome_lock:
            if not self._should_send_welcome(chat_name):
                return False
            self._send_welcome(
                chat_name,
                sender,
                msg_type,
                policy,
                content,
                chat,
                dry_run_reply,
                message_ids,
            )
            return True

    def _send_welcome(
        self,
        chat_name: str,
        sender: str,
        msg_type: str,
        policy: str,
        content: str,
        chat: Any,
        dry_run_reply: bool,
        message_ids: list[int],
    ) -> None:
        welcome = self.config.welcome_message.strip()
        history = self.histories[chat_name]
        if dry_run_reply:
            if not self._mark_messages_generated(message_ids, welcome):
                return
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
        if self.store:
            self._enqueue_and_send_outbox(
                message_ids=message_ids,
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=welcome,
                kind="welcome",
                chat=chat,
            )
            return
        try:
            self._run_wechat_ui("send_welcome", lambda: chat.SendMsg(welcome))
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
            if not content or not (
                self._message_type_is_text(msg_type)
                or self._message_type_is_voice(msg_type)
                or self._message_is_image_like(msg_type, content)
            ):
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
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            safe_line = line.encode("gbk", errors="replace").decode("gbk")
            print(safe_line, flush=True)
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
        if self.paused:
            self._write_health("paused", f"last_action={action}")
        else:
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

    def _heartbeat(self, message: str) -> None:
        gate = self._wechat_operation_gate.snapshot()
        if gate.active and gate.timed_out:
            self._write_health("blocked", f"timed-out WeChat operation still running: {gate.operation_name}")
            return
        now = time.time()
        if now - self.last_health_at < HEALTH_HEARTBEAT_INTERVAL_SECONDS:
            return
        self.last_health_at = now
        self._write_health("running", message)


class _PollingChat:
    def __init__(
        self,
        wx: Any,
        who: str,
        switch_before_send: bool = True,
        retry_on_error: bool = True,
    ) -> None:
        self.wx = wx
        self.who = who
        self.switch_before_send = switch_before_send
        self.retry_on_error = retry_on_error

    def SendMsg(self, message: str) -> None:
        if self.switch_before_send:
            self._switch_to_chat()
        try:
            self.wx.SendMsg(message)
        except Exception:
            if not self.retry_on_error or not self.switch_before_send:
                raise
            time.sleep(0.8)
            self._switch_to_chat()
            time.sleep(0.2)
            self.wx.SendMsg(message)

    def _switch_to_chat(self) -> None:
        chat_with = getattr(self.wx, "ChatWith")
        try:
            chat_with(self.who, exact=True, force=True, force_wait=0.2)
        except TypeError:
            chat_with(self.who)
        time.sleep(0.1)


class _BufferedMessage:
    def __init__(self, content: str, sender: str, msg_type: str) -> None:
        self.content = content
        self.sender = sender
        self.type = msg_type
