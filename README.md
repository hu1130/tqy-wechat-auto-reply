# wechat-AI

This repo restores an older local prototype into a small WeChat auto-reply project.

It keeps the original two experiments:

- `demo.py`: LLM output prototype
- `wxdemo.py`: WeChat automation prototype

And adds a real glue layer so the bot can:

1. listen to selected chats with `wxauto`
2. build a short conversation prompt
3. call an LLM
4. strip hidden `<think>...</think>` sections
5. send the final reply back to WeChat

## Current status

- The code path is repaired and organized.
- Local logic can be tested on macOS.
- Real WeChat automation still requires Windows because `wxauto` only supports Windows WeChat desktop.

## Project layout

```text
wechat-AI/
├── main.py                 # unified CLI
├── demo.py                 # LLM-only quick test wrapper
├── wxdemo.py               # WeChat bot wrapper
├── wechat_ai/
│   ├── config.py
│   ├── llm.py
│   ├── bot.py
│   └── utils.py
├── tests/
│   └── test_utils.py
├── .env.example
└── ARCHIVE.md
```

## Requirements

- Python 3.9+
- Windows desktop WeChat for real auto-reply mode
- `wxauto` on Windows
- An LLM endpoint

The default config uses local Ollama:

- endpoint: `http://localhost:11434`
- model: `deepseek-r1:14b`

## Install

### macOS / general logic testing

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Windows for real WeChat auto-reply

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Then edit `.env`:

- set `DRY_RUN=0`
- set `WECHAT_LISTEN_CHATS` to the nickname or group name you want to monitor
- if needed, set `WECHAT_SELF_NAMES`

## Usage

### 1. Test the LLM only

```bash
python main.py demo "你好，今天忙什么呢？"
```

or:

```bash
python demo.py "你好，今天忙什么呢？"
```

### 2. Show loaded config

```bash
python main.py config
```

### 3. Run the WeChat bot

Dry-run first:

```bash
python main.py run --dry-run
```

Real reply mode on Windows:

```bash
python main.py run
```

or:

```bash
python wxdemo.py
```

## Notes

- `wxauto` listening mode is callback-based. This repo uses `AddListenChat(...)` and `KeepRunning()`.
- Duplicate messages are filtered in a short time window to reduce repeated replies.
- Conversation context is kept in memory per chat.
- `<think>...</think>` content is stripped before sending replies back.

## Tests

```bash
python -m unittest discover -s tests -v
```

