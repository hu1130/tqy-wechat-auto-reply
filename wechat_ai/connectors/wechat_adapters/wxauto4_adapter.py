from __future__ import annotations

import platform
from typing import Any

from wechat_ai.connectors.wechat_adapters.base import (
    WeChatAdapterCapabilities,
    WeChatAdapterStatus,
)


class Wxauto4WeChatAdapter:
    adapter_name = "wxauto4_generic"

    def __init__(self, wx: Any | None = None) -> None:
        self.wx = wx

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
        online = bool(getattr(self.wx, "IsOnline", lambda: True)())
        account_name = ""
        try:
            info = self.wx.GetMyInfo()
            if isinstance(info, dict):
                account_name = str(info.get("nickname") or info.get("name") or "")
        except Exception:
            account_name = ""
        return WeChatAdapterStatus(
            adapter_name=self.adapter_name,
            online=online,
            account_name=account_name,
            client_path=str(getattr(self.wx, "path", "") or ""),
            client_dir=str(getattr(self.wx, "dir", "") or ""),
            capabilities=self._capabilities(),
            details={"class": type(self.wx).__name__},
        )

    def compatibility_report(self) -> dict[str, Any]:
        self.connect()
        capabilities = self._capabilities()
        required = {"session_scan", "receive_text", "send_text", "polling_api"}
        return {
            "adapter": self.adapter_name,
            "online": bool(getattr(self.wx, "IsOnline", lambda: True)()),
            "capabilities": capabilities.__dict__,
            "missing": [name for name, supported in capabilities.__dict__.items() if not supported and name in required],
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
            voice_to_text=True,
            image_download=True,
            listener_api=callable(getattr(wx, "AddListenChat", None)) and callable(getattr(wx, "KeepRunning", None)),
            polling_api=all(callable(getattr(wx, name, None)) for name in ("ChatWith", "GetAllMessage", "SendMsg")),
        )

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
