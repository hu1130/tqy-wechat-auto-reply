from __future__ import annotations

import json
import time
from typing import Any

from wechat_ai.config import AppConfig
from wechat_ai.connectors.protocol import InboundMessage, OutboundMessage
from wechat_ai.connectors.registry import ConnectorDefinition, ConnectorRegistry
from wechat_ai.connectors.wechat_desktop import WeChatDesktopConnector


DEFAULT_CONNECTOR_CHANNEL = "wechat_desktop"
DEFAULT_CONNECTOR_ID = "local-wechat-desktop"


def build_wechat_desktop_connector(config: AppConfig, connector_id: str = DEFAULT_CONNECTOR_ID) -> WeChatDesktopConnector:
    scan_all = any(chat.strip() == "*" for chat in config.wechat_listen_chats)
    listen_chats = [] if scan_all else config.wechat_listen_chats
    return WeChatDesktopConnector(
        connector_id=connector_id,
        scan_all_sessions=scan_all,
        listen_chats=listen_chats,
    )


def build_connector_registry() -> ConnectorRegistry:
    registry = ConnectorRegistry()
    registry.register(
        ConnectorDefinition(
            channel=DEFAULT_CONNECTOR_CHANNEL,
            description="Windows desktop Weixin/WeChat connector powered by wxauto4.",
            builder=build_wechat_desktop_connector,
        )
    )
    return registry


def list_connectors() -> list[dict[str, str]]:
    return [
        {"channel": item.channel, "description": item.description}
        for item in build_connector_registry().list()
    ]


def build_connector(
    config: AppConfig,
    channel: str = DEFAULT_CONNECTOR_CHANNEL,
    connector_id: str = DEFAULT_CONNECTOR_ID,
):
    return build_connector_registry().build(channel=channel, config=config, connector_id=connector_id)


def connector_status(
    config: AppConfig,
    connector_id: str = DEFAULT_CONNECTOR_ID,
    channel: str = DEFAULT_CONNECTOR_CHANNEL,
) -> dict[str, Any]:
    connector = build_connector(config, channel=channel, connector_id=connector_id)
    return connector.status().__dict__


def connector_compatibility(
    config: AppConfig,
    connector_id: str = DEFAULT_CONNECTOR_ID,
    channel: str = DEFAULT_CONNECTOR_CHANNEL,
) -> dict[str, Any]:
    connector = build_connector(config, channel=channel, connector_id=connector_id)
    compatibility_report = getattr(connector, "compatibility_report", None)
    if not callable(compatibility_report):
        raise RuntimeError(f"Connector {channel} does not expose a compatibility report.")
    report = compatibility_report()
    report.update(
        {
            "connector_id": connector_id,
            "channel": connector.channel,
            "scan_all_sessions": connector.scan_all_sessions,
            "listen_chats": connector.listen_chats,
        }
    )
    return report


def run_local_connector_agent(
    config: AppConfig,
    connector_id: str = DEFAULT_CONNECTOR_ID,
    channel: str = DEFAULT_CONNECTOR_CHANNEL,
    interval_seconds: float = 1.0,
    echo: bool = False,
) -> None:
    connector = build_connector(config, channel=channel, connector_id=connector_id)
    status = connector.status()
    print(json.dumps({"event": "connector_status", **status.__dict__}, ensure_ascii=False), flush=True)
    connector.seed()
    print(json.dumps({"event": "connector_seeded", "connector_id": connector_id}, ensure_ascii=False), flush=True)

    while True:
        for message in connector.poll():
            print(json.dumps({"event": "inbound_message", **_message_dict(message)}, ensure_ascii=False), flush=True)
            if echo:
                connector.send(
                    OutboundMessage(
                        connector_id=message.connector_id,
                        channel=message.channel,
                        conversation_id=message.conversation_id,
                        content=f"[echo] {message.content}",
                    )
                )
        time.sleep(interval_seconds)


def _message_dict(message: InboundMessage) -> dict[str, Any]:
    return {
        "connector_id": message.connector_id,
        "channel": message.channel,
        "conversation_id": message.conversation_id,
        "sender": message.sender,
        "message_type": message.message_type,
        "content": message.content,
        "message_id": message.message_id,
        "raw": message.raw,
    }
