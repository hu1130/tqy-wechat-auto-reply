from __future__ import annotations

import time
from typing import Any

from wechat_ai.connectors.protocol import (
    ConnectorCommand,
    ConnectorEvent,
    ConnectorStatus,
    InboundMessage,
    OutboundMessage,
)
from wechat_ai.connectors.wechat_adapters.base import WeChatDesktopAdapter
from wechat_ai.connectors.wechat_adapters.wxauto4_adapter import Wxauto4WeChatAdapter


class WeChatDesktopConnector:
    channel = "wechat_desktop"

    def __init__(
        self,
        connector_id: str = "local-wechat-desktop",
        wx: Any | None = None,
        adapter: WeChatDesktopAdapter | None = None,
        scan_all_sessions: bool = True,
        listen_chats: list[str] | None = None,
    ) -> None:
        self.connector_id = connector_id
        self.adapter = adapter or Wxauto4WeChatAdapter(wx=wx)
        self.scan_all_sessions = scan_all_sessions
        self.listen_chats = listen_chats or []
        self.seen: dict[str, set[str]] = {}
        self.session_signatures: dict[str, str] = {}

    def connect(self) -> None:
        self.adapter.connect()

    def status(self) -> ConnectorStatus:
        self.connect()
        adapter_status = self.adapter.status()
        return ConnectorStatus(
            connector_id=self.connector_id,
            channel=self.channel,
            online=adapter_status.online,
            account_name=adapter_status.account_name,
            details={
                "adapter": adapter_status.adapter_name,
                "capabilities": adapter_status.capabilities.__dict__,
                "client_path": adapter_status.client_path,
                "client_dir": adapter_status.client_dir,
                "scan_all_sessions": self.scan_all_sessions,
                "listen_chats": self.listen_chats,
                **adapter_status.details,
            },
        )

    def compatibility_report(self) -> dict[str, Any]:
        return self.adapter.compatibility_report()

    def poll(self) -> list[InboundMessage]:
        self.connect()
        if self.scan_all_sessions:
            return self._poll_visible_sessions()
        messages: list[InboundMessage] = []
        for chat_name in self.listen_chats:
            messages.extend(self._poll_chat(chat_name))
        return messages

    def send(self, message: OutboundMessage) -> None:
        self.connect()
        if message.channel != self.channel:
            raise RuntimeError(f"Cannot send {message.channel} through {self.channel}.")
        self.adapter.chat_with(message.conversation_id)
        self.adapter.send_text(message.content)

    def handle_command(self, command: ConnectorCommand) -> ConnectorEvent:
        if command.command == "send_message":
            self.send(
                OutboundMessage(
                    connector_id=self.connector_id,
                    channel=self.channel,
                    conversation_id=command.conversation_id,
                    content=command.content,
                    raw=command.payload,
                )
            )
            return ConnectorEvent("message_sent", self.connector_id, self.channel, {"conversation_id": command.conversation_id})
        if command.command in {"status", "ping"}:
            return ConnectorEvent("status", self.connector_id, self.channel, self.status().__dict__)
        return ConnectorEvent("unsupported_command", self.connector_id, self.channel, {"command": command.command})

    def seed(self) -> None:
        self.connect()
        if self.scan_all_sessions:
            for session in self._get_sessions():
                chat_name = self.adapter.session_name(session)
                if chat_name:
                    self._seed_chat(chat_name)
            return
        for chat_name in self.listen_chats:
            self._seed_chat(chat_name)

    def _poll_visible_sessions(self) -> list[InboundMessage]:
        messages: list[InboundMessage] = []
        for session in self._get_sessions():
            chat_name = self.adapter.session_name(session)
            if not chat_name:
                continue
            signature = self.adapter.session_signature(session)
            is_new_session = chat_name not in self.seen
            changed = self.session_signatures.get(chat_name) != signature
            has_unread = self.adapter.session_has_unread(session)
            self.session_signatures[chat_name] = signature
            if is_new_session:
                self._seed_chat(chat_name)
                continue
            if changed or has_unread:
                messages.extend(self._poll_chat(chat_name))
        return messages

    def _seed_chat(self, chat_name: str) -> None:
        try:
            self.adapter.chat_with(chat_name)
            self.seen.setdefault(chat_name, set()).update(
                self.adapter.message_key(msg) for msg in self.adapter.get_all_messages()
            )
        except Exception:
            self.seen.setdefault(chat_name, set())

    def _poll_chat(self, chat_name: str) -> list[InboundMessage]:
        self.adapter.chat_with(chat_name)
        self.seen.setdefault(chat_name, set())
        inbound: list[InboundMessage] = []
        for msg in self.adapter.get_all_messages():
            key = self.adapter.message_key(msg)
            if key in self.seen[chat_name]:
                continue
            self.seen[chat_name].add(key)
            content = self.adapter.message_content(msg)
            if not content:
                continue
            sender = str(getattr(msg, "sender", "")).strip()
            msg_type = str(getattr(msg, "type", "")).strip() or "text"
            inbound.append(
                InboundMessage(
                    connector_id=self.connector_id,
                    channel=self.channel,
                    conversation_id=chat_name,
                    sender=sender or chat_name,
                    message_type=msg_type,
                    content=content,
                    message_id=key,
                    raw={"attr": str(getattr(msg, "attr", "")), "received_at": time.time()},
                )
            )
        return inbound

    def _get_sessions(self) -> list[Any]:
        return self.adapter.get_sessions()

    @staticmethod
    def _message_key(msg: Any) -> str:
        return Wxauto4WeChatAdapter._message_key(msg)

    @staticmethod
    def _message_content(msg: Any) -> str:
        return Wxauto4WeChatAdapter._message_content(msg)

    @staticmethod
    def _looks_like_voice_placeholder(content: str) -> bool:
        return Wxauto4WeChatAdapter._looks_like_voice_placeholder(content)

    @staticmethod
    def _session_name(session: Any) -> str:
        return Wxauto4WeChatAdapter._session_name(session)

    @staticmethod
    def _session_signature(session: Any) -> str:
        return Wxauto4WeChatAdapter._session_signature(session)

    @staticmethod
    def _session_has_unread(session: Any) -> bool:
        return Wxauto4WeChatAdapter._session_has_unread(session)
