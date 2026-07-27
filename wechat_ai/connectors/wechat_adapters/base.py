from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class WeChatAdapterCapabilities:
    session_scan: bool = False
    receive_text: bool = False
    send_text: bool = False
    voice_to_text: bool = False
    image_download: bool = False
    listener_api: bool = False
    polling_api: bool = False
    contact_snapshot: bool = False
    friend_request_scan: bool = False
    friend_request_accept: bool = False
    friend_request_reject: bool = False


@dataclass(frozen=True)
class WeChatAdapterStatus:
    adapter_name: str
    online: bool
    account_name: str = ""
    client_path: str = ""
    client_dir: str = ""
    capabilities: WeChatAdapterCapabilities = field(default_factory=WeChatAdapterCapabilities)
    details: dict[str, Any] = field(default_factory=dict)


class WeChatDesktopAdapter(Protocol):
    adapter_name: str

    def connect(self) -> None:
        ...

    def status(self) -> WeChatAdapterStatus:
        ...

    def compatibility_report(self) -> dict[str, Any]:
        ...

    def get_sessions(self) -> list[Any]:
        ...

    def chat_with(self, chat_name: str) -> None:
        ...

    def get_all_messages(self) -> list[Any]:
        ...

    def send_text(self, content: str) -> None:
        ...

    def snapshot_contacts(self) -> list[dict[str, Any]]:
        ...

    def snapshot_friend_requests(self) -> list[dict[str, Any]]:
        ...

    def execute_operation(self, operation_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    def session_name(self, session: Any) -> str:
        ...

    def session_signature(self, session: Any) -> str:
        ...

    def session_has_unread(self, session: Any) -> bool:
        ...

    def message_key(self, message: Any) -> str:
        ...

    def message_content(self, message: Any) -> str:
        ...
