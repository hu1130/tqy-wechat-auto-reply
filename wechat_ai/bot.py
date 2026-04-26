from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
import platform
from typing import Any

from wechat_ai.config import AppConfig
from wechat_ai.llm import LLMClient
from wechat_ai.utils import MessageDeduplicator, append_history


class WeChatAutoReplyBot:
    def __init__(self, config: AppConfig, llm_client: LLMClient) -> None:
        self.config = config
        self.llm_client = llm_client
        self.histories: dict[str, deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=self.config.max_context_messages * 2)
        )
        self.deduplicator = MessageDeduplicator(ttl_seconds=self.config.dedup_window_seconds)

    def start(self) -> None:
        wx = self._create_wechat_client()
        chat_names = self.config.wechat_listen_chats or ["文件传输助手"]

        self._log(f"Listening chats: {', '.join(chat_names)}")
        if self.config.dry_run:
            self._log("Dry-run mode is ON. Replies will not be sent.")

        for chat_name in chat_names:
            wx.AddListenChat(chat_name, self._on_message)

        wx.KeepRunning()

    def _create_wechat_client(self) -> Any:
        if platform.system() != "Windows":
            raise RuntimeError(
                "Real WeChat auto-reply requires Windows desktop WeChat because wxauto only supports Windows."
            )
        try:
            from wxauto import WeChat
        except ImportError as exc:
            raise RuntimeError(
                "wxauto is not installed. Run `pip install -r requirements.txt` on Windows."
            ) from exc
        return WeChat()

    def _on_message(self, msg: Any, chat: Any) -> None:
        content = str(getattr(msg, "content", "")).strip()
        sender = str(getattr(msg, "sender", "")).strip()
        msg_type = str(getattr(msg, "type", "")).strip()
        chat_name = self._chat_name(chat)

        if not content:
            return
        if self._is_self_message(msg, sender):
            return
        if self.deduplicator.is_duplicate(chat_name, sender, content, msg_type):
            self._log(f"Skip duplicate message from {chat_name}: {content}")
            return

        self._log(f"Received from {chat_name} ({sender or 'unknown'}): {content}")
        history = self.histories[chat_name]
        append_history(history, role="user", sender=sender or chat_name, content=content)

        reply = self.llm_client.generate_reply(chat_name=chat_name, history=list(history)).strip()
        if not reply:
            self._log(f"LLM returned empty reply for {chat_name}")
            return

        append_history(history, role="assistant", sender="assistant", content=reply)
        if self.config.dry_run:
            self._log(f"[dry-run] Reply to {chat_name}: {reply}")
            return

        chat.SendMsg(reply)
        self._log(f"Sent reply to {chat_name}: {reply}")

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

    @staticmethod
    def _log(message: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] {message}", flush=True)

