from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import sqlite3
from typing import Any


@dataclass(frozen=True)
class ChatState:
    chat_name: str
    policy: str = ""
    paused: bool = False
    pause_reason: str = ""
    last_reply_at: float = 0.0


class SQLiteStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_state (
                    chat_name TEXT PRIMARY KEY,
                    policy TEXT NOT NULL DEFAULT '',
                    paused INTEGER NOT NULL DEFAULT 0,
                    pause_reason TEXT NOT NULL DEFAULT '',
                    last_reply_at REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    action TEXT NOT NULL,
                    chat_name TEXT NOT NULL DEFAULT '',
                    sender TEXT NOT NULL DEFAULT '',
                    msg_type TEXT NOT NULL DEFAULT '',
                    policy TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    sent INTEGER,
                    content_preview TEXT NOT NULL DEFAULT '',
                    reply_preview TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_chat_ts ON events(chat_name, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_action_ts ON events(action, ts)")

    def get_chat_state(self, chat_name: str) -> ChatState:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT chat_name, policy, paused, pause_reason, last_reply_at
                FROM chat_state
                WHERE chat_name = ?
                """,
                (chat_name,),
            ).fetchone()
        if row is None:
            return ChatState(chat_name=chat_name)
        return ChatState(
            chat_name=str(row["chat_name"]),
            policy=str(row["policy"]),
            paused=bool(row["paused"]),
            pause_reason=str(row["pause_reason"]),
            last_reply_at=float(row["last_reply_at"]),
        )

    def set_chat_policy(self, chat_name: str, policy: str) -> None:
        self._upsert_chat_state(chat_name=chat_name, policy=policy)

    def pause_chat(self, chat_name: str, reason: str = "manual") -> None:
        self._upsert_chat_state(chat_name=chat_name, paused=True, pause_reason=reason)

    def resume_chat(self, chat_name: str) -> None:
        self._upsert_chat_state(chat_name=chat_name, paused=False, pause_reason="")

    def touch_last_reply(self, chat_name: str, timestamp: float) -> None:
        self._upsert_chat_state(chat_name=chat_name, last_reply_at=timestamp)

    def record_event(self, event: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    ts, action, chat_name, sender, msg_type, policy, reason, sent,
                    content_preview, reply_preview
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.get("ts", self._now())),
                    str(event.get("action", "")),
                    str(event.get("chat_name", "")),
                    str(event.get("sender", "")),
                    str(event.get("msg_type", "")),
                    str(event.get("policy", "")),
                    str(event.get("reason", "")),
                    self._sent_value(event.get("sent")),
                    str(event.get("content_preview", "")),
                    str(event.get("reply_preview", "")),
                ),
            )

    def count_sent(self, report_date: str | None = None, chat_name: str = "") -> int:
        selected_date = report_date or date.today().isoformat()
        sql = "SELECT COUNT(*) AS count FROM events WHERE action = 'send' AND sent = 1 AND substr(ts, 1, 10) = ?"
        params: list[object] = [selected_date]
        if chat_name:
            sql += " AND chat_name = ?"
            params.append(chat_name)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["count"]) if row else 0

    def daily_summary(self, report_date: str | None = None) -> dict[str, object]:
        selected_date = report_date or date.today().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT action, chat_name, reason, sent
                FROM events
                WHERE substr(ts, 1, 10) = ?
                """,
                (selected_date,),
            ).fetchall()

        action_counts: Counter[str] = Counter()
        skip_reasons: Counter[str] = Counter()
        sent_by_chat: Counter[str] = Counter()
        total = 0
        for row in rows:
            total += 1
            action = str(row["action"])
            reason = str(row["reason"])
            chat_name = str(row["chat_name"])
            action_counts[action] += 1
            if action == "skip" and reason:
                skip_reasons[reason] += 1
            if action == "send" and bool(row["sent"]):
                sent_by_chat[chat_name or "(unknown)"] += 1

        return {
            "date": selected_date,
            "total_events": total,
            "actions": dict(sorted(action_counts.items())),
            "skip_reasons": dict(skip_reasons.most_common(10)),
            "sent_by_chat": dict(sent_by_chat.most_common(10)),
        }

    def list_chat_states(self) -> list[ChatState]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_name, policy, paused, pause_reason, last_reply_at
                FROM chat_state
                ORDER BY chat_name
                """
            ).fetchall()
        return [
            ChatState(
                chat_name=str(row["chat_name"]),
                policy=str(row["policy"]),
                paused=bool(row["paused"]),
                pause_reason=str(row["pause_reason"]),
                last_reply_at=float(row["last_reply_at"]),
            )
            for row in rows
        ]

    def _upsert_chat_state(
        self,
        chat_name: str,
        policy: str | None = None,
        paused: bool | None = None,
        pause_reason: str | None = None,
        last_reply_at: float | None = None,
    ) -> None:
        current = self.get_chat_state(chat_name)
        next_policy = current.policy if policy is None else policy
        next_paused = current.paused if paused is None else paused
        next_pause_reason = current.pause_reason if pause_reason is None else pause_reason
        next_last_reply_at = current.last_reply_at if last_reply_at is None else last_reply_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_state (
                    chat_name, policy, paused, pause_reason, last_reply_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_name) DO UPDATE SET
                    policy = excluded.policy,
                    paused = excluded.paused,
                    pause_reason = excluded.pause_reason,
                    last_reply_at = excluded.last_reply_at,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name,
                    next_policy,
                    1 if next_paused else 0,
                    next_pause_reason,
                    next_last_reply_at,
                    self._now(),
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _sent_value(value: object) -> int | None:
        if value is None:
            return None
        return 1 if bool(value) else 0

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")


def format_daily_report(summary: dict[str, object]) -> str:
    lines = [
        f"Daily report for {summary['date']}",
        f"Total events: {summary['total_events']}",
        "",
        "Actions:",
    ]
    actions = summary.get("actions", {})
    if isinstance(actions, dict) and actions:
        lines.extend(f"- {key}: {value}" for key, value in actions.items())
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Top skip reasons:")
    skip_reasons = summary.get("skip_reasons", {})
    if isinstance(skip_reasons, dict) and skip_reasons:
        lines.extend(f"- {key}: {value}" for key, value in skip_reasons.items())
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Sent by chat:")
    sent_by_chat = summary.get("sent_by_chat", {})
    if isinstance(sent_by_chat, dict) and sent_by_chat:
        lines.extend(f"- {key}: {value}" for key, value in sent_by_chat.items())
    else:
        lines.append("- none")
    return "\n".join(lines)
