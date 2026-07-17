# WeChat AI Auto Reply - Handoff

## Current Goal

Build a Windows desktop connector based AI customer service assistant. The current production path is a WeChat/Weixin 4.x desktop automation connector using `wxauto4`, plus a local dashboard and a connector abstraction that can later support more channels.

## Current Runtime

- Python: 3.10 on Windows.
- Entry point: `main.py`.
- Main bot: `wechat_ai/bot.py`.
- AI API adapter: `wechat_ai/llm.py`.
- Local state: SQLite via `wechat_ai/store.py`.
- Dashboard: `wechat_ai/web_admin.py`.
- Connector abstraction: `wechat_ai/connectors/`.
- Knowledge file: `knowledge/faq.md`.

## Important Configuration

Use `.env.example` as the template. Do not commit or share the real `.env`.

The current real deployment uses:

- `LLM_PROVIDER=openai_compatible`
- `OPENAI_BASE_URL=https://aics.setdx.com/v1/chat`
- empty model name
- a custom API shape implemented in `wechat_ai/llm.py`

The custom API expects:

```json
{
  "api_key": "...",
  "session_id": "wechat_chat_name_or_session_id",
  "message": "customer message",
  "preferred_language": "zh-CN"
}
```

It returns `messages[].text`, which the bot sends back to WeChat.

## Implemented Features

- WeChat/Weixin 4.x desktop attachment via `wxauto4`.
- `--chat "*"` session scan mode for visible WeChat conversation list.
- Automatic reply with live/dry-run modes.
- Local dashboard with login, customer list, pause/resume, policy editing, reports.
- Local SQLite persistence for chat state, customer metadata, welcome sent state, events.
- Opening welcome message, sent once per customer.
- Message buffering for customers who send multiple messages quickly.
- Cooldown handling that delays buffered replies instead of dropping messages.
- Duplicate message protection.
- Reply splitting into multiple WeChat messages with delay.
- Basic voice-to-text support through wxauto4 `to_text` when available.
- Group-like chat filtering, disabled by default unless explicitly allowed.
- Self-message detection to avoid replying to the bot's own messages.
- Startup scan protections:
  - Do not send welcome if latest visible valid message is from self.
  - Send welcome only when a customer message exists and welcome was not already sent.
- Current chat recheck after polling so customer messages that arrive while bot is sending are less likely to be missed.
- Connector protocol scaffold for future channels.

## Known Limitations

- This is desktop automation, not an official WeChat API. It can break with WeChat updates and carries account risk.
- Session scanning depends on visible conversations in the desktop client.
- It is not a true concurrent multi-channel engine yet; it is polling and switching the desktop UI.
- Voice-to-text depends on wxauto4/WeChat exposing transcription and is not a robust ASR pipeline.
- Image understanding is not implemented yet.
- Long AI API latency can still make replies feel slow.
- The bot does not yet interrupt an in-progress multi-part reply when the customer sends a new message mid-send; it rechecks after the current handling pass.
- Current customer identity is chat-name based; nickname changes may look like a new customer.

## Recent Fixes Worth Preserving

- Custom AI API request shape in `wechat_ai/llm.py`.
- Opening welcome message persistence in SQLite.
- Avoid welcome replay for conversations where latest visible message is self.
- Buffered messages are no longer dropped just because cooldown is still active.
- Polling rechecks the current chat after handling to catch interleaved messages.
- Removed weak group heuristic that treated "俱乐部" as group chat.

## Suggested Next Plan

1. Reduce account risk.
   - Prefer replying only after customer initiates.
   - Add per-hour and per-day throttles.
   - Make proactive welcome optional and conservative.

2. Improve reliability.
   - Add a durable per-chat pending queue.
   - Add message IDs/timestamps if wxauto4 exposes them.
   - Persist unprocessed messages before calling the AI API.
   - Add timeout/fallback handling around send and AI calls.

3. Improve UX.
   - Add dashboard controls for welcome on/off, rate limits, allowed chats, blocked chats.
   - Add lead scoring and handoff indicators.
   - Add logs/search in the dashboard.

4. Expand connectors.
   - Keep desktop connector for WeChat/enterprise desktop clients.
   - Add official API connectors where possible.
   - Normalize inbound/outbound events using `wechat_ai/connectors/protocol.py`.

5. Add media understanding.
   - Implement robust voice ASR outside wxauto4 if needed.
   - Add image OCR/image understanding pipeline.

## Commands

Install/setup:

```powershell
.\setup_windows.ps1
```

Doctor:

```powershell
.\.venv\Scripts\python.exe main.py doctor --skip-llm
```

Run all visible chats live:

```powershell
.\.venv\Scripts\python.exe main.py run --live
```

Run one chat dry-run:

```powershell
.\.venv\Scripts\python.exe main.py run --dry-run --chat "文件传输助手"
```

Dashboard:

```powershell
.\.venv\Scripts\python.exe main.py dashboard --host 127.0.0.1 --port 8787
```

Tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Packaging Notes

The handoff zip intentionally excludes:

- `.env`
- `.venv/`
- `data/`
- `logs/`
- `wxauto_logs/`
- `__pycache__/`
- `*.pyc`

