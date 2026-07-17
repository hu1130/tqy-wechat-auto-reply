from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class InboundMessage:
    connector_id: str
    channel: str
    conversation_id: str
    sender: str
    message_type: str
    content: str
    message_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundMessage:
    connector_id: str
    channel: str
    conversation_id: str
    content: str
    message_type: str = "text"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorStatus:
    connector_id: str
    channel: str
    online: bool
    account_name: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorCommand:
    command: str
    connector_id: str
    channel: str
    conversation_id: str = ""
    content: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorEvent:
    event: str
    connector_id: str
    channel: str
    payload: dict[str, Any] = field(default_factory=dict)


class DesktopConnector(Protocol):
    connector_id: str
    channel: str

    def status(self) -> ConnectorStatus:
        ...

    def poll(self) -> list[InboundMessage]:
        ...

    def send(self, message: OutboundMessage) -> None:
        ...

    def handle_command(self, command: ConnectorCommand) -> ConnectorEvent:
        ...
