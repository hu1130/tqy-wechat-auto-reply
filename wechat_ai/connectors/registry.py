from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from wechat_ai.config import AppConfig
from wechat_ai.connectors.protocol import DesktopConnector


ConnectorBuilder = Callable[[AppConfig, str], DesktopConnector]


@dataclass(frozen=True)
class ConnectorDefinition:
    channel: str
    description: str
    builder: ConnectorBuilder


class ConnectorRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ConnectorDefinition] = {}

    def register(self, definition: ConnectorDefinition) -> None:
        if definition.channel in self._definitions:
            raise RuntimeError(f"Connector channel already registered: {definition.channel}")
        self._definitions[definition.channel] = definition

    def list(self) -> list[ConnectorDefinition]:
        return sorted(self._definitions.values(), key=lambda item: item.channel)

    def get(self, channel: str) -> ConnectorDefinition:
        try:
            return self._definitions[channel]
        except KeyError as exc:
            supported = ", ".join(sorted(self._definitions)) or "<none>"
            raise RuntimeError(f"Unsupported connector channel: {channel}. Supported channels: {supported}") from exc

    def build(self, channel: str, config: AppConfig, connector_id: str) -> DesktopConnector:
        return self.get(channel).builder(config, connector_id)
