# WeChat 4.x Test Guide

This release uses `wxauto4==41.1.2` with Windows desktop WeChat 4.x. It is a version-sensitive UI automation test path, not a long-term stable official WeChat integration.

## Prerequisites

- Windows 10 or Windows 11, 64-bit.
- Python 3.9 through 3.13, 64-bit.
- Current Windows WeChat is logged in and its main window is visible.
- PowerShell and WeChat run in the same signed-in Windows session with the same privilege level.

## Install and Check

Run these commands from the project directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_test_environment.ps1
.\.venv\Scripts\python.exe main.py doctor --skip-llm
```

Only continue when `wxauto4` reports `[OK]`.

If the doctor reports `DLL load failed`, `application control policy blocked this file`, or a similar error, stop testing and ask the device administrator to review the `wxauto4` package. Do not change Windows security policy or attempt to bypass login or update restrictions.

## First Dry-run

1. Set `WECHAT_LISTEN_CHATS` and `REPLY_ALLOWED_CHATS` in `.env` to one internal test chat.
2. Keep `DRY_RUN=1`, `REQUIRE_MANUAL_CONFIRM=1`, and low send limits.
3. Send a message to that chat from a second test WeChat account.
4. Run:

```powershell
.\.venv\Scripts\python.exe main.py run --dry-run --chat "internal test chat"
```

5. Review `logs\wechat-ai.log` and `logs\audit.jsonl`. Confirm that the reply is generated and no real message is sent.

Run the dry-run again after every desktop WeChat update. If attachment fails, wait for or switch to a `wxauto4` package compatible with that WeChat build.
