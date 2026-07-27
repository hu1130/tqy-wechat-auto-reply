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
from wechat_ai.wechat_runtime import OperationGate


class WeChatDesktopConnector:
    channel = "wechat_desktop"

    def __init__(
        self,
        connector_id: str = "local-wechat-desktop",
        wx: Any | None = None,
        adapter: WeChatDesktopAdapter | None = None,
        scan_all_sessions: bool = True,
        listen_chats: list[str] | None = None,
        operation_timeout_seconds: float = 15.0,
        explicit_client_version: str = "",
        verified_version_prefixes: list[str] | tuple[str, ...] = ("4.1.8.107",),
    ) -> None:
        self.connector_id = connector_id
        self.adapter = adapter or Wxauto4WeChatAdapter(
            wx=wx,
            explicit_client_version=explicit_client_version,
            verified_version_prefixes=verified_version_prefixes,
        )
        self.scan_all_sessions = scan_all_sessions
        self.listen_chats = listen_chats or []
        self.seen: dict[str, set[str]] = {}
        self.session_signatures: dict[str, str] = {}
        self.operation_timeout_seconds = float(operation_timeout_seconds)
        if self.operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._operation_gate = OperationGate()

    def connect(self) -> None:
        self.adapter.connect()

    def status(self) -> ConnectorStatus:
        self.connect()
        adapter_status = self.adapter.status()
        compatible = bool(adapter_status.details.get("compatible", False))
        return ConnectorStatus(
            connector_id=self.connector_id,
            channel=self.channel,
            online=adapter_status.online and compatible,
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
        return self._run_ui("poll", self._poll_unlocked)

    def _poll_unlocked(self) -> list[InboundMessage]:
        self.connect()
        self._require_compatible()
        if self.scan_all_sessions:
            return self._poll_visible_sessions()
        messages: list[InboundMessage] = []
        for chat_name in self.listen_chats:
            messages.extend(self._poll_chat(chat_name))
        return messages

    def send(self, message: OutboundMessage) -> None:
        self._run_ui("send", lambda: self._send_unlocked(message))

    def _send_unlocked(self, message: OutboundMessage) -> None:
        self.connect()
        self._require_compatible()
        if message.channel != self.channel:
            raise RuntimeError(f"Cannot send {message.channel} through {self.channel}.")
        self.adapter.chat_with(message.conversation_id)
        self.adapter.send_text(message.content)

    def snapshot_contacts(self) -> list[dict[str, Any]]:
        self._require_compatible()
        capabilities = self.adapter.status().capabilities
        if not capabilities.contact_snapshot:
            raise RuntimeError("WeChat adapter cannot produce a contact snapshot")
        return self._run_ui("snapshot_contacts", self.adapter.snapshot_contacts)

    def snapshot_friend_requests(self) -> list[dict[str, Any]]:
        self._require_compatible()
        capabilities = self.adapter.status().capabilities
        if not capabilities.friend_request_scan:
            raise RuntimeError("WeChat adapter cannot scan friend requests")
        return self._run_ui("snapshot_friend_requests", self.adapter.snapshot_friend_requests)

    def execute_operation(self, operation_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"accept_friend", "reject_friend", "send_welcome"}
        if operation_type not in allowed:
            raise ValueError(f"unsupported WeChat operation type: {operation_type}")
        self._require_compatible()
        capabilities = self.adapter.status().capabilities
        capability_name = {
            "accept_friend": "friend_request_accept",
            "reject_friend": "friend_request_reject",
            "send_welcome": "send_text",
        }[operation_type]
        if not bool(getattr(capabilities, capability_name, False)):
            raise ValueError(
                f"WeChat adapter capability {capability_name} is unavailable"
            )
        return self._run_ui(
            f"operation:{operation_type}",
            lambda: self.adapter.execute_operation(operation_type, payload),
        )

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
        if command.command == "snapshot_contacts":
            return ConnectorEvent(
                "contacts_snapshot",
                self.connector_id,
                self.channel,
                {"contacts": self.snapshot_contacts()},
            )
        if command.command == "snapshot_friend_requests":
            return ConnectorEvent(
                "friend_requests_snapshot",
                self.connector_id,
                self.channel,
                {"requests": self.snapshot_friend_requests()},
            )
        if command.command in {"accept_friend", "reject_friend", "send_welcome"}:
            result = self.execute_operation(command.command, command.payload)
            return ConnectorEvent("operation_completed", self.connector_id, self.channel, result)
        return ConnectorEvent("unsupported_command", self.connector_id, self.channel, {"command": command.command})

    def seed(self) -> None:
        self._run_ui("seed", self._seed_unlocked)

    def _seed_unlocked(self) -> None:
        self.connect()
        self._require_compatible()
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
            baseline_keys = {
                self.adapter.message_key(msg) for msg in self.adapter.get_all_messages()
            }
        except Exception as exc:
            raise RuntimeError(
                f"Failed to establish connector startup baseline for {chat_name}: {exc}"
            ) from exc
        self.seen.setdefault(chat_name, set()).update(baseline_keys)

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

    def _run_ui(self, operation_name: str, operation: Any) -> Any:
        return self._operation_gate.run(
            operation_name,
            operation,
            timeout_seconds=self.operation_timeout_seconds,
        )

    def _require_compatible(self) -> None:
        status = self.adapter.status()
        version = str(status.details.get("client_version") or "unknown")
        if not status.online:
            raise RuntimeError("WeChat desktop client is offline")
        if not bool(status.details.get("compatible", False)):
            missing = status.details.get("missing_required_capabilities") or []
            if missing:
                raise RuntimeError(
                    "WeChat adapter required capabilities are unavailable: "
                    + ",".join(str(item) for item in missing)
                )
            raise RuntimeError(
                f"WeChat client version {version} is not in the verified compatibility list"
            )

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
