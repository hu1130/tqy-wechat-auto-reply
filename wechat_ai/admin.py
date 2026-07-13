from __future__ import annotations

from wechat_ai.config import ALLOWED_CHAT_POLICIES, AppConfig
from wechat_ai.ops import save_report
from wechat_ai.store import SQLiteStore, format_daily_report


def get_store(config: AppConfig) -> SQLiteStore:
    if not config.state_db_file:
        raise RuntimeError("STATE_DB_FILE is empty. Set it to enable admin commands.")
    return SQLiteStore(config.state_db_file)


def render_status(config: AppConfig) -> str:
    store = get_store(config)
    states = store.list_chat_states()
    lines = [
        f"State DB: {config.state_db_file}",
        f"Default policy: {config.default_chat_policy}",
        f"Allowed chats: {', '.join(config.reply_allowed_chats) or '(none)'}",
        "",
        "Tracked chats:",
    ]
    if not states:
        lines.append("- none")
        return "\n".join(lines)

    for state in states:
        policy = state.policy or "(config/default)"
        paused = "paused" if state.paused else "active"
        reason = f", reason={state.pause_reason}" if state.pause_reason else ""
        lines.append(f"- {state.chat_name}: {paused}, policy={policy}{reason}")
    return "\n".join(lines)


def pause_chat(config: AppConfig, chat_name: str, reason: str) -> str:
    store = get_store(config)
    store.pause_chat(chat_name, reason=reason or "manual")
    return f"Paused {chat_name}: {reason or 'manual'}"


def resume_chat(config: AppConfig, chat_name: str) -> str:
    store = get_store(config)
    store.resume_chat(chat_name)
    return f"Resumed {chat_name}"


def set_chat_policy(config: AppConfig, chat_name: str, policy: str) -> str:
    normalized = policy.strip().lower()
    if normalized not in ALLOWED_CHAT_POLICIES:
        allowed = ", ".join(sorted(ALLOWED_CHAT_POLICIES))
        raise RuntimeError(f"Policy must be one of: {allowed}")
    store = get_store(config)
    store.set_chat_policy(chat_name, normalized)
    return f"Set {chat_name} policy to {normalized}"


def render_report(config: AppConfig, report_date: str | None = None) -> str:
    store = get_store(config)
    return format_daily_report(store.daily_summary(report_date=report_date))


def save_daily_report(config: AppConfig, report_date: str | None = None) -> str:
    store = get_store(config)
    summary = store.daily_summary(report_date=report_date)
    report_text = format_daily_report(summary)
    path = save_report(config.reports_dir, report_text, str(summary["date"]))
    return f"Saved report to {path}"
