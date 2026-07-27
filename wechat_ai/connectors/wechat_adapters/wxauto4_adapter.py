from __future__ import annotations

import hashlib
import platform
import unicodedata
from collections.abc import Mapping
from typing import Any

from wechat_ai.contacts import scan_channel_contacts
from wechat_ai.connectors.wechat_adapters.base import (
    WeChatAdapterCapabilities,
    WeChatAdapterStatus,
)
from wechat_ai.friend_requests import execute_friend_request_action, scan_friend_requests
from wechat_ai.wechat_runtime import detect_wechat_client_version


FULL_REQUIRED_CAPABILITIES = {
    "session_scan",
    "receive_text",
    "send_text",
    "polling_api",
    "contact_snapshot",
    "friend_request_scan",
    "friend_request_accept",
    "friend_request_reject",
}

_ACCOUNT_ID_KEYS = ("wxid", "wechatid", "account", "username", "id")
_NICKNAME_KEYS = ("nickname", "name", "displayname")
_MAX_IDENTITY_TEXT = 256


def _normalized_identity_key(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _identity_text(value: object) -> str:
    if type(value) is not str:
        return ""
    value = value.strip()
    if not value or len(value) > _MAX_IDENTITY_TEXT:
        return ""
    if any(unicodedata.category(character) == "Cc" for character in value):
        return ""
    return value


def _extract_wechat_identity(info: object) -> dict[str, str]:
    if not isinstance(info, Mapping):
        return {}
    normalized: dict[str, object] = {}
    for key, value in info.items():
        if type(key) is not str:
            continue
        normalized.setdefault(_normalized_identity_key(key), value)

    def first(keys: tuple[str, ...]) -> str:
        for key in keys:
            value = _identity_text(normalized.get(key))
            if value:
                return value
        return ""

    identity: dict[str, str] = {}
    account_id = first(_ACCOUNT_ID_KEYS)
    nickname = first(_NICKNAME_KEYS)
    if account_id:
        identity["account_id"] = account_id
    if nickname:
        identity["nickname"] = nickname
    return identity


class Wxauto4WeChatAdapter:
    adapter_name = "wxauto4_generic"

    def __init__(
        self,
        wx: Any | None = None,
        *,
        explicit_client_version: str = "",
        verified_version_prefixes: list[str] | tuple[str, ...] = ("4.1.8.107",),
    ) -> None:
        self.wx = wx
        self.explicit_client_version = str(explicit_client_version).strip()
        self.verified_version_prefixes = tuple(
            value.strip() for value in verified_version_prefixes if value.strip()
        )
        if not self.verified_version_prefixes:
            raise ValueError("verified_version_prefixes must not be empty")

    def connect(self) -> None:
        if self.wx is not None:
            return
        if platform.system() != "Windows":
            raise RuntimeError("wxauto4 WeChat adapter requires Windows.")
        try:
            from wxauto4 import WeChat
        except ImportError as exc:
            raise RuntimeError("wxauto4 is not installed.") from exc
        self.wx = WeChat(ads=False, resize=False)

    def status(self) -> WeChatAdapterStatus:
        self.connect()
        status_error = ""
        try:
            online = bool(getattr(self.wx, "IsOnline", lambda: True)())
        except Exception as exc:
            online = False
            status_error = str(exc)[:500]
        client_version = detect_wechat_client_version(
            self.wx,
            self.explicit_client_version,
        )
        capabilities = self._capabilities()
        missing = [
            name
            for name in FULL_REQUIRED_CAPABILITIES
            if not bool(getattr(capabilities, name, False))
        ]
        compatible = bool(
            client_version
            and any(
                client_version.startswith(prefix)
                for prefix in self.verified_version_prefixes
            )
            and not missing
        )
        wechat_identity: dict[str, str] = {}
        try:
            info = self.wx.GetMyInfo()
            wechat_identity = _extract_wechat_identity(info)
        except Exception:
            wechat_identity = {}
        account_name = wechat_identity.get("nickname", "")
        return WeChatAdapterStatus(
            adapter_name=self.adapter_name,
            online=online,
            account_name=account_name,
            client_path=str(getattr(self.wx, "path", "") or ""),
            client_dir=str(getattr(self.wx, "dir", "") or ""),
            capabilities=capabilities,
            details={
                "class": type(self.wx).__name__,
                "client_version": client_version,
                "verified_version_prefixes": list(self.verified_version_prefixes),
                "compatible": compatible,
                "missing_required_capabilities": sorted(missing),
                "status_error": status_error,
                "wechat_identity": wechat_identity,
            },
        )

    def compatibility_report(self) -> dict[str, Any]:
        self.connect()
        capabilities = self._capabilities()
        required = FULL_REQUIRED_CAPABILITIES
        client_version = detect_wechat_client_version(
            self.wx,
            self.explicit_client_version,
        )
        version_supported = bool(
            client_version
            and any(client_version.startswith(prefix) for prefix in self.verified_version_prefixes)
        )
        try:
            online = bool(getattr(self.wx, "IsOnline", lambda: True)())
            status_error = ""
        except Exception as exc:
            online = False
            status_error = str(exc)[:500]
        return {
            "adapter": self.adapter_name,
            "online": online,
            "capabilities": capabilities.__dict__,
            "missing": [name for name, supported in capabilities.__dict__.items() if not supported and name in required],
            "client_version": client_version,
            "verified_version_prefixes": list(self.verified_version_prefixes),
            "version_supported": version_supported,
            "status_error": status_error,
            "safe_to_run": bool(
                version_supported
                and online
                and not [name for name, supported in capabilities.__dict__.items() if not supported and name in required]
            ),
        }

    def get_sessions(self) -> list[Any]:
        self.connect()
        get_session = getattr(self.wx, "GetSession", None)
        if not callable(get_session):
            raise RuntimeError("wxauto4 does not expose GetSession.")
        return list(get_session() or [])

    def chat_with(self, chat_name: str) -> None:
        self.connect()
        self.wx.ChatWith(chat_name)

    def get_all_messages(self) -> list[Any]:
        self.connect()
        return list(self.wx.GetAllMessage() or [])

    def send_text(self, content: str) -> None:
        self.connect()
        self.wx.SendMsg(content)

    def snapshot_contacts(self) -> list[dict[str, Any]]:
        """Scan the contacts UI and enrich it with wxauto4 session metadata."""

        self.connect()
        contacts = scan_channel_contacts(wechat=self.wx)
        sessions_by_name: dict[str, tuple[Any, dict[str, Any]]] = {}
        for session in self.get_sessions():
            info = getattr(session, "info", None)
            metadata = info if isinstance(info, dict) else {}
            name = self.session_name(session)
            if not name:
                continue
            sessions_by_name.setdefault(name, (session, metadata))

        seen: set[str] = set()
        enriched: list[dict[str, Any]] = []
        for contact in contacts:
            value = dict(contact)
            raw = dict(value.get("raw") or {})
            session_data = sessions_by_name.pop(
                str(value.get("display_name") or ""),
                None,
            )
            if session_data is not None:
                session, metadata = session_data
                raw_identifier = self._session_external_id(session, metadata)
                if raw_identifier:
                    value["external_contact_id"] = "wxc_" + hashlib.sha256(
                        raw_identifier.encode("utf-8")
                    ).hexdigest()[:32]
                    raw["identity_quality"] = "wechat_id"
                    raw["wechat_identifier"] = raw_identifier
                raw["source"] = "wechat_contacts_ui+wxauto4_sessions"
            value["raw"] = raw
            external_contact_id = str(value["external_contact_id"])
            if external_contact_id not in seen:
                seen.add(external_contact_id)
                enriched.append(value)

        for name, (session, metadata) in sessions_by_name.items():
            raw_identifier = self._session_external_id(session, metadata)
            identity_quality = "wechat_id" if raw_identifier else "display_name"
            identifier_source = raw_identifier or name
            external_contact_id = "wxc_" + hashlib.sha256(
                identifier_source.encode("utf-8")
            ).hexdigest()[:32]
            if external_contact_id in seen:
                continue
            seen.add(external_contact_id)
            enriched.append(
                {
                    "external_contact_id": external_contact_id,
                    "display_name": name,
                    "remark": str(metadata.get("remark") or getattr(session, "remark", "") or ""),
                    "kind": self._session_kind(session, metadata, raw_identifier),
                    "status": "active",
                    "raw": {
                        "source": "wxauto4_sessions",
                        "coverage": "recent_session_only",
                        "identity_quality": identity_quality,
                        "wechat_identifier": raw_identifier,
                    },
                }
            )
        return enriched

    def snapshot_friend_requests(self) -> list[dict[str, Any]]:
        self.connect()
        return [item.to_dict() for item in scan_friend_requests(wechat=self.wx)]

    def execute_operation(self, operation_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.connect()
        if operation_type in {"accept_friend", "reject_friend"}:
            request_id = str(payload.get("external_request_id") or "").strip()
            if not request_id:
                raise ValueError("friend operation payload requires external_request_id")
            result = execute_friend_request_action(
                request_id,
                operation_type,
                wechat=self.wx,
            )
            return {
                "external_request_id": result.external_request_id,
                "operation_type": result.operation_type,
                "status": result.status,
                "evidence": result.evidence,
            }
        if operation_type == "send_welcome":
            conversation_id = str(
                payload.get("conversation_external_id")
                or payload.get("display_name")
                or payload.get("external_contact_id")
                or ""
            ).strip()
            content = str(payload.get("content") or "").strip()
            if not conversation_id or not content:
                raise ValueError("send_welcome requires conversation_external_id and content")
            self.chat_with(conversation_id)
            self.send_text(content)
            return {
                "operation_type": operation_type,
                "status": "succeeded",
                "evidence": {"conversation_external_id": conversation_id},
            }
        raise ValueError(f"unsupported WeChat operation type: {operation_type}")

    def session_name(self, session: Any) -> str:
        return self._session_name(session)

    def session_signature(self, session: Any) -> str:
        return self._session_signature(session)

    def session_has_unread(self, session: Any) -> bool:
        return self._session_has_unread(session)

    def message_key(self, message: Any) -> str:
        return self._message_key(message)

    def message_content(self, message: Any) -> str:
        return self._message_content(message)

    def _capabilities(self) -> WeChatAdapterCapabilities:
        wx = self.wx
        return WeChatAdapterCapabilities(
            session_scan=callable(getattr(wx, "GetSession", None)),
            receive_text=callable(getattr(wx, "GetAllMessage", None)),
            send_text=callable(getattr(wx, "SendMsg", None)),
            voice_to_text=False,
            image_download=False,
            listener_api=callable(getattr(wx, "AddListenChat", None)) and callable(getattr(wx, "KeepRunning", None)),
            polling_api=all(callable(getattr(wx, name, None)) for name in ("ChatWith", "GetAllMessage", "SendMsg")),
            contact_snapshot=callable(getattr(wx, "SwitchToContact", None)),
            friend_request_scan=callable(getattr(wx, "SwitchToContact", None)),
            friend_request_accept=callable(getattr(wx, "SwitchToContact", None)),
            friend_request_reject=callable(getattr(wx, "SwitchToContact", None)),
        )

    @staticmethod
    def _session_external_id(session: Any, info: dict[str, Any]) -> str:
        for key in ("wxid", "user_name", "username", "user_id", "id"):
            value = info.get(key)
            if value:
                return str(value).strip()
            value = getattr(session, key, "")
            if value:
                return str(value).strip()
        return ""

    @staticmethod
    def _session_kind(session: Any, info: dict[str, Any], external_id: str) -> str:
        if bool(info.get("is_group") or getattr(session, "is_group", False)):
            return "group"
        type_text = str(info.get("type") or getattr(session, "type", "") or "").lower()
        if "group" in type_text or external_id.endswith("@chatroom"):
            return "group"
        return "friend"

    @staticmethod
    def _message_key(msg: Any) -> str:
        msg_id = getattr(msg, "id", None)
        if msg_id:
            return f"id:{msg_id}"
        return "|".join(
            [
                type(msg).__name__,
                str(getattr(msg, "attr", "")),
                str(getattr(msg, "sender", "")),
                str(getattr(msg, "type", "")),
                str(getattr(msg, "content", "")),
            ]
        )

    @staticmethod
    def _message_content(msg: Any) -> str:
        content = str(getattr(msg, "content", "")).strip()
        if content and not Wxauto4WeChatAdapter._looks_like_voice_placeholder(content):
            return content
        to_text = getattr(msg, "to_text", None)
        if callable(to_text):
            try:
                text = str(to_text()).strip()
                if text:
                    return text
            except Exception:
                pass
        return content

    @staticmethod
    def _looks_like_voice_placeholder(content: str) -> bool:
        return bool(
            content
            and (
                ("\u8bed\u97f3" in content and "\u672a\u64ad\u653e" in content)
                or ("\u7487\uE5E2\u7176" in content and "\u93C8\uE041\u6331" in content)
            )
        )

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
            return "|".join(str(info.get(key, "")) for key in ("name", "time", "content", "isnew", "new_count"))
        return "|".join(str(getattr(session, key, "")) for key in ("name", "time", "content", "isnew", "new_count"))

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
