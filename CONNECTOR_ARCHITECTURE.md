# Connector Architecture

This project now has a first-pass connector architecture for stage 1 and stage 2.

## Stage 1: Desktop Connectors

The existing WeChat automation remains available through:

```powershell
.\.venv\Scripts\python.exe main.py run --live
```

The new connector agent surface is available through:

```powershell
.\.venv\Scripts\python.exe main.py connector status
.\.venv\Scripts\python.exe main.py connector doctor
.\.venv\Scripts\python.exe main.py connector list
.\.venv\Scripts\python.exe main.py connector run-local
```

The first desktop connector is:

```text
wechat_desktop
```

It wraps the local Windows WeChat client through `wxauto4`.

## Stage 2: Unified Connector Protocol

Shared protocol models live in:

```text
wechat_ai/connectors/protocol.py
```

Core models:

```text
InboundMessage
OutboundMessage
ConnectorStatus
ConnectorCommand
ConnectorEvent
DesktopConnector
```

Connector registration lives in:

```text
wechat_ai/connectors/registry.py
```

The runtime builds connectors through:

```text
wechat_ai/connector_agent.py
```

Current channel registration:

```text
wechat_desktop -> WeChatDesktopConnector
```

All future channels should map their native messages into `InboundMessage`:

```json
{
  "connector_id": "local-wechat-desktop",
  "channel": "wechat_desktop",
  "conversation_id": "KiraAI",
  "sender": "customer",
  "message_type": "text",
  "content": "hello"
}
```

And send through `OutboundMessage`:

```json
{
  "connector_id": "local-wechat-desktop",
  "channel": "wechat_desktop",
  "conversation_id": "KiraAI",
  "message_type": "text",
  "content": "hello, how can I help?"
}
```

## Next Channels

Recommended next connector modules:

```text
wechat_ai/connectors/wecom_desktop.py
wechat_ai/connectors/browser_web.py
wechat_ai/connectors/official_api.py
```

The SaaS backend can later communicate with the local connector agent through WebSocket or HTTPS polling.
