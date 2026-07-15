from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
import json
import platform
import time
from typing import Any

from wechat_ai.config import AppConfig
from wechat_ai.knowledge import KnowledgeBase
from wechat_ai.llm import LLMClient
from wechat_ai.ops import append_alert, append_rotating_line, redact_sensitive_text, write_health_file
from wechat_ai.store import SQLiteStore
from wechat_ai.utils import MessageDeduplicator, append_history


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

    def _create_wechat_client(self) -> Any:
        if platform.system() != "Windows":
            raise RuntimeError(
                "Real WeChat auto-reply requires Windows desktop WeChat because wxauto only supports Windows. "
                "Run this project on Windows, then execute `python main.py doctor`."
            )
        try:
            from wxauto import WeChat
        except ImportError as exc:
            raise RuntimeError(
                "wxauto is not installed. Run `pip install -r requirements.txt` on Windows."
            ) from exc
        return WeChat()

    def _on_message(self, msg: Any, chat: Any) -> None:
        if self.paused:
            self._log("Bot is paused after repeated errors. Restart it after fixing the issue.")
            self._audit("skip", reason="bot_paused")
            return

        content = str(getattr(msg, "content", "")).strip()
        sender = str(getattr(msg, "sender", "")).strip()
        msg_type = str(getattr(msg, "type", "")).strip()
        chat_name = self._chat_name(chat)
        policy = self._chat_policy(chat_name)

        if not content:
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

        reply_block_reason = self._reply_block_reason(reply)
        if reply_block_reason:
            self._log(f"Skip reply to {chat_name}: {reply_block_reason}")
            self._audit(
                "skip",
                chat_name=chat_name,
                sender=sender,
                msg_type=msg_type,
                policy=policy,
                content=content,
                reply=reply,
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
            chat.SendMsg(reply)
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
        if normalized_type in set(self.config.text_message_types):
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
        if self.config.reply_allowed_chats and chat_name not in set(self.config.reply_allowed_chats):
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
        if self.config.reply_allowed_senders and sender not in set(self.config.reply_allowed_senders):
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

    def _is_self_message(self, msg: Any, sender: str) -> bool:
        for attr_name in ("is_self", "isSelf", "from_self", "fromSelf", "self"):
            attr_value = getattr(msg, attr_name, None)
            if attr_value is True:
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
