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


@dataclass(frozen=True)
class Customer:
    chat_name: str
    display_name: str = ""
    phone: str = ""
    source: str = "wechat_desktop"
    tags: str = ""
    lead_level: str = "unknown"
    status: str = "new"
    owner: str = ""
    notes: str = ""
    welcome_sent_at: str = ""
    updated_at: str = ""
    created_at: str = ""


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    chat_name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'wechat_desktop',
                    tags TEXT NOT NULL DEFAULT '',
                    lead_level TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL DEFAULT 'new',
                    owner TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    welcome_sent_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "customers", "welcome_sent_at", "TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_chat_ts ON events(chat_name, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_action_ts ON events(action, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_lead ON customers(lead_level, status)")

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

    def has_sent_welcome(self, chat_name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT welcome_sent_at FROM customers WHERE chat_name = ?",
                (chat_name,),
            ).fetchone()
        return bool(row and str(row["welcome_sent_at"]))

    def mark_welcome_sent(self, chat_name: str, timestamp: str | None = None) -> None:
        now = timestamp or self._now()
        with self._connect() as conn:
            existing = conn.execute("SELECT chat_name FROM customers WHERE chat_name = ?", (chat_name,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE customers SET welcome_sent_at = ?, updated_at = ? WHERE chat_name = ?",
                    (now, now, chat_name),
                )
                return
            conn.execute(
                """
                INSERT INTO customers (
                    chat_name, display_name, source, lead_level, status,
                    welcome_sent_at, updated_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_name, chat_name, "wechat_desktop", "unknown", "new", now, now, now),
            )

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
            chat_name = str(event.get("chat_name", "")).strip()
            if chat_name:
                self._upsert_customer_from_event(conn, event)

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

    def list_recent_events(self, limit: int = 100, chat_name: str = "") -> list[dict[str, Any]]:
        sql = """
            SELECT id, ts, action, chat_name, sender, msg_type, policy, reason, sent,
                   content_preview, reply_preview
            FROM events
        """
        params: list[object] = []
        if chat_name:
            sql += " WHERE chat_name = ?"
            params.append(chat_name)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": int(row["id"]),
                "ts": str(row["ts"]),
                "action": str(row["action"]),
                "chat_name": str(row["chat_name"]),
                "sender": str(row["sender"]),
                "msg_type": str(row["msg_type"]),
                "policy": str(row["policy"]),
                "reason": str(row["reason"]),
                "sent": None if row["sent"] is None else bool(row["sent"]),
                "content_preview": str(row["content_preview"]),
                "reply_preview": str(row["reply_preview"]),
            }
            for row in rows
        ]

    def chat_overview(self, limit: int = 100) -> list[dict[str, Any]]:
        states = {state.chat_name: state for state in self.list_chat_states()}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_name,
                       MAX(ts) AS last_event_at,
                       SUM(CASE WHEN action = 'send' AND sent = 1 THEN 1 ELSE 0 END) AS sent_count,
                       SUM(CASE WHEN action = 'skip' THEN 1 ELSE 0 END) AS skip_count
                FROM events
                WHERE chat_name != ''
                GROUP BY chat_name
                ORDER BY last_event_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        names = set(states)
        names.update(str(row["chat_name"]) for row in rows)
        by_name = {str(row["chat_name"]): row for row in rows}
        overview: list[dict[str, Any]] = []
        def last_event_at(name: str) -> str:
            row = by_name.get(name)
            return str(row["last_event_at"]) if row else ""

        for chat_name in sorted(names, key=last_event_at, reverse=True):
            state = states.get(chat_name, ChatState(chat_name=chat_name))
            row = by_name.get(chat_name)
            overview.append(
                {
                    "chat_name": chat_name,
                    "policy": state.policy,
                    "paused": state.paused,
                    "pause_reason": state.pause_reason,
                    "last_reply_at": state.last_reply_at,
                    "last_event_at": str(row["last_event_at"]) if row else "",
                    "sent_count": int(row["sent_count"] or 0) if row else 0,
                    "skip_count": int(row["skip_count"] or 0) if row else 0,
                }
            )
        return overview

    def upsert_customer(
        self,
        chat_name: str,
        display_name: str = "",
        phone: str = "",
        source: str = "wechat_desktop",
        tags: str = "",
        lead_level: str = "unknown",
        status: str = "new",
        owner: str = "",
        notes: str = "",
    ) -> None:
        if not chat_name.strip():
            raise RuntimeError("chat_name is required.")
        now = self._now()
        with self._connect() as conn:
            existing = conn.execute("SELECT created_at FROM customers WHERE chat_name = ?", (chat_name,)).fetchone()
            conn.execute(
                """
                INSERT INTO customers (
                    chat_name, display_name, phone, source, tags, lead_level,
                    status, owner, notes, welcome_sent_at, updated_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_name) DO UPDATE SET
                    display_name = excluded.display_name,
                    phone = excluded.phone,
                    source = excluded.source,
                    tags = excluded.tags,
                    lead_level = excluded.lead_level,
                    status = excluded.status,
                    owner = excluded.owner,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name.strip(),
                    display_name.strip(),
                    phone.strip(),
                    source.strip() or "wechat_desktop",
                    tags.strip(),
                    lead_level.strip() or "unknown",
                    status.strip() or "new",
                    owner.strip(),
                    notes.strip(),
                    self.get_customer(chat_name).welcome_sent_at if existing else "",
                    now,
                    str(existing["created_at"]) if existing else now,
                ),
            )

    def get_customer(self, chat_name: str) -> Customer:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT chat_name, display_name, phone, source, tags, lead_level,
                       status, owner, notes, welcome_sent_at, updated_at, created_at
                FROM customers
                WHERE chat_name = ?
                """,
                (chat_name,),
            ).fetchone()
        if row is None:
            return Customer(chat_name=chat_name)
        return self._customer_from_row(row)

    def list_customers(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.chat_name, c.display_name, c.phone, c.source, c.tags,
                       c.lead_level, c.status, c.owner, c.notes, c.welcome_sent_at, c.updated_at, c.created_at,
                       MAX(e.ts) AS last_event_at,
                       SUM(CASE WHEN e.action = 'send' AND e.sent = 1 THEN 1 ELSE 0 END) AS sent_count,
                       SUM(CASE WHEN e.action = 'skip' THEN 1 ELSE 0 END) AS skip_count
                FROM customers c
                LEFT JOIN events e ON e.chat_name = c.chat_name
                GROUP BY c.chat_name
                ORDER BY COALESCE(last_event_at, c.updated_at) DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [
            {
                **self._customer_from_row(row).__dict__,
                "last_event_at": str(row["last_event_at"] or ""),
                "sent_count": int(row["sent_count"] or 0),
                "skip_count": int(row["skip_count"] or 0),
            }
            for row in rows
        ]

    def _upsert_customer_from_event(self, conn: sqlite3.Connection, event: dict[str, Any]) -> None:
        chat_name = str(event.get("chat_name", "")).strip()
        if not chat_name:
            return
        now = str(event.get("ts") or self._now())
        content = f"{event.get('content_preview', '')} {event.get('reply_preview', '')}"
        inferred_level = self._infer_lead_level(content)
        existing = conn.execute(
            "SELECT lead_level, status, created_at FROM customers WHERE chat_name = ?",
            (chat_name,),
        ).fetchone()
        if existing:
            next_level = self._higher_lead_level(str(existing["lead_level"]), inferred_level)
            conn.execute(
                """
                UPDATE customers
                SET lead_level = ?, updated_at = ?
                WHERE chat_name = ?
                """,
                (next_level, now, chat_name),
            )
            return
        conn.execute(
            """
            INSERT INTO customers (
                chat_name, display_name, source, lead_level, status, updated_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_name, chat_name, "wechat_desktop", inferred_level, "new", now, now),
        )

    @staticmethod
    def _infer_lead_level(text: str) -> str:
        high_keywords = ("价格", "报价", "多少钱", "收费", "购买", "合同", "付款", "合作", "试用", "演示")
        medium_keywords = ("了解", "介绍", "产品", "方案", "功能", "案例", "怎么做", "可以")
        if any(keyword in text for keyword in high_keywords):
            return "high"
        if any(keyword in text for keyword in medium_keywords):
            return "medium"
        return "unknown"

    @staticmethod
    def _higher_lead_level(current: str, inferred: str) -> str:
        rank = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
        return inferred if rank.get(inferred, 0) > rank.get(current, 0) else current

    @staticmethod
    def _customer_from_row(row: sqlite3.Row) -> Customer:
        return Customer(
            chat_name=str(row["chat_name"]),
            display_name=str(row["display_name"]),
            phone=str(row["phone"]),
            source=str(row["source"]),
            tags=str(row["tags"]),
            lead_level=str(row["lead_level"]),
            status=str(row["status"]),
            owner=str(row["owner"]),
            notes=str(row["notes"]),
            welcome_sent_at=str(row["welcome_sent_at"]),
            updated_at=str(row["updated_at"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
