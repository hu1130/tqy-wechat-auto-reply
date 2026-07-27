from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator


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


@dataclass(frozen=True)
class FailedReply:
    id: int
    chat_name: str
    reply: str
    sender: str = ""
    msg_type: str = ""
    policy: str = ""
    content_preview: str = ""
    error: str = ""
    attempts: int = 0
    status: str = "pending"
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class StoredMessage:
    id: int
    fingerprint: str
    chat_name: str
    sender: str
    msg_type: str
    content: str
    status: str
    reply: str = ""
    error: str = ""
    received_at: str = ""
    updated_at: str = ""
    batch_id: int | None = None
    batch_position: int | None = None
    resolved_content: str = ""


@dataclass(frozen=True)
class MessageBatch:
    id: int
    chat_name: str
    status: str
    deadline_at: float
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class MessageMedia:
    message_id: int
    kind: str
    source_path: str
    managed_path: str
    checksum: str
    size_bytes: int
    status: str
    extracted_text: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class OutboxJob:
    id: int
    dedupe_key: str
    source_message_ids: tuple[int, ...]
    chat_name: str
    sender: str
    msg_type: str
    policy: str
    content_preview: str
    reply: str
    kind: str
    status: str
    attempts: int
    sent_parts: int = 0
    next_attempt_at: float = 0.0
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS failed_replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    reply TEXT NOT NULL,
                    sender TEXT NOT NULL DEFAULT '',
                    msg_type TEXT NOT NULL DEFAULT '',
                    policy TEXT NOT NULL DEFAULT '',
                    content_preview TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'collecting'
                        CHECK(status IN ('collecting', 'processing', 'blocked', 'completed', 'failed')),
                    deadline_at REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_message_batches_blocked_status(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    chat_name TEXT NOT NULL,
                    sender TEXT NOT NULL DEFAULT '',
                    msg_type TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'received'
                        CHECK(status IN ('received', 'processing', 'generated', 'sending', 'sent', 'failed')),
                    reply TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    batch_id INTEGER REFERENCES message_batches(id),
                    batch_position INTEGER,
                    resolved_content TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_column(conn, "messages", "batch_id", "INTEGER")
            self._ensure_column(conn, "messages", "batch_position", "INTEGER")
            self._ensure_column(conn, "messages", "resolved_content", "TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_media (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('voice', 'image')),
                    source_path TEXT NOT NULL DEFAULT '',
                    managed_path TEXT NOT NULL DEFAULT '',
                    checksum TEXT NOT NULL DEFAULT '',
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'ready', 'processed', 'failed')),
                    extracted_text TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "message_media", "size_bytes", "INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    source_message_ids TEXT NOT NULL,
                    chat_name TEXT NOT NULL,
                    sender TEXT NOT NULL DEFAULT '',
                    msg_type TEXT NOT NULL DEFAULT '',
                    policy TEXT NOT NULL DEFAULT '',
                    content_preview TEXT NOT NULL DEFAULT '',
                    reply TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'reply',
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued', 'sending', 'sent', 'uncertain')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    sent_parts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "outbox", "sent_parts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "outbox", "next_attempt_at", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "customers", "welcome_sent_at", "TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_chat_ts ON events(chat_name, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_action_ts ON events(action, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_lead ON customers(lead_level, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_failed_replies_status ON failed_replies(status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_batch ON messages(batch_id, batch_position, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_message_batches_status ON message_batches(status, id)")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_message_batches_collecting_chat
                ON message_batches(chat_name)
                WHERE status = 'collecting'
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, id)")

    def record_message(
        self,
        fingerprint: str,
        chat_name: str,
        sender: str = "",
        msg_type: str = "",
        content: str = "",
    ) -> tuple[StoredMessage, bool]:
        normalized_fingerprint = fingerprint.strip()
        if not normalized_fingerprint:
            raise RuntimeError("fingerprint is required.")
        now = self._now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    fingerprint, chat_name, sender, msg_type, content, status,
                    reply, error, received_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'received', '', '', ?, ?)
                ON CONFLICT(fingerprint) DO NOTHING
                """,
                (
                    normalized_fingerprint,
                    chat_name,
                    sender,
                    msg_type,
                    content,
                    now,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                """
                SELECT id, fingerprint, chat_name, sender, msg_type, content,
                       status, reply, error, received_at, updated_at, batch_id,
                       batch_position, resolved_content
                FROM messages
                WHERE fingerprint = ?
                """,
                (normalized_fingerprint,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to record message.")
        return self._message_from_row(row), created

    def get_message(self, message_id: int) -> StoredMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, fingerprint, chat_name, sender, msg_type, content,
                       status, reply, error, received_at, updated_at, batch_id,
                       batch_position, resolved_content
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
        return self._message_from_row(row) if row else None

    def get_message_by_fingerprint(self, fingerprint: str) -> StoredMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, fingerprint, chat_name, sender, msg_type, content,
                       status, reply, error, received_at, updated_at, batch_id,
                       batch_position, resolved_content
                FROM messages
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
        return self._message_from_row(row) if row else None

    def list_messages(self, status: str, limit: int = 100) -> list[StoredMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, fingerprint, chat_name, sender, msg_type, content,
                       status, reply, error, received_at, updated_at, batch_id,
                       batch_position, resolved_content
                FROM messages
                WHERE status = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (status, max(1, limit)),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def record_buffered_message(
        self,
        fingerprint: str,
        chat_name: str,
        sender: str,
        msg_type: str,
        content: str,
        deadline_at: float,
    ) -> tuple[StoredMessage, MessageBatch, bool]:
        normalized_fingerprint = fingerprint.strip()
        normalized_chat_name = chat_name.strip()
        if not normalized_fingerprint:
            raise RuntimeError("fingerprint is required.")
        if not normalized_chat_name:
            raise RuntimeError("chat_name is required.")
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            message_row = conn.execute(
                """
                SELECT id, fingerprint, chat_name, sender, msg_type, content,
                       status, reply, error, received_at, updated_at, batch_id,
                       batch_position, resolved_content
                FROM messages
                WHERE fingerprint = ?
                """,
                (normalized_fingerprint,),
            ).fetchone()
            created = message_row is None
            if message_row is not None and message_row["batch_id"] is not None:
                batch_row = self._select_message_batch(conn, int(message_row["batch_id"]))
                if batch_row is None:
                    raise RuntimeError("Message references a missing batch.")
                return (
                    self._message_from_row(message_row),
                    self._message_batch_from_row(batch_row),
                    False,
                )

            effective_chat_name = (
                normalized_chat_name
                if message_row is None
                else str(message_row["chat_name"])
            )
            batch_id = self._get_or_create_collecting_batch(
                conn,
                effective_chat_name,
                float(deadline_at),
                now,
                update_existing_deadline=created,
            )
            batch_position = self._next_batch_position(conn, batch_id)
            if message_row is None:
                cursor = conn.execute(
                    """
                    INSERT INTO messages (
                        fingerprint, chat_name, sender, msg_type, content, status,
                        reply, error, received_at, updated_at, batch_id,
                        batch_position, resolved_content
                    )
                    VALUES (?, ?, ?, ?, ?, 'received', '', '', ?, ?, ?, ?, '')
                    """,
                    (
                        normalized_fingerprint,
                        normalized_chat_name,
                        sender,
                        msg_type,
                        content,
                        now,
                        now,
                        batch_id,
                        batch_position,
                    ),
                )
                message_id = int(cursor.lastrowid)
            else:
                message_id = int(message_row["id"])
                cursor = conn.execute(
                    """
                    UPDATE messages
                    SET batch_id = ?, batch_position = ?, updated_at = ?
                    WHERE id = ? AND batch_id IS NULL
                    """,
                    (batch_id, batch_position, now, message_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Message could not be appended to the batch.")

            message_row = conn.execute(
                """
                SELECT id, fingerprint, chat_name, sender, msg_type, content,
                       status, reply, error, received_at, updated_at, batch_id,
                       batch_position, resolved_content
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
            batch_row = self._select_message_batch(conn, batch_id)
        if message_row is None or batch_row is None:
            raise RuntimeError("Failed to record buffered message.")
        return (
            self._message_from_row(message_row),
            self._message_batch_from_row(batch_row),
            created,
        )

    def append_message_to_batch(
        self,
        message_id: int,
        chat_name: str,
        deadline_at: float,
    ) -> MessageBatch:
        normalized_chat_name = chat_name.strip()
        if not normalized_chat_name:
            raise RuntimeError("chat_name is required.")
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            message = conn.execute(
                "SELECT chat_name, batch_id FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if message is None:
                raise RuntimeError("Message does not exist.")
            if str(message["chat_name"]) != normalized_chat_name:
                raise RuntimeError("Message chat_name does not match the batch chat_name.")

            existing_batch_id = message["batch_id"]
            if existing_batch_id is not None:
                row = conn.execute(
                    """
                    SELECT id, chat_name, status, deadline_at, error, created_at, updated_at
                    FROM message_batches
                    WHERE id = ?
                    """,
                    (int(existing_batch_id),),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Message references a missing batch.")
                return self._message_batch_from_row(row)

            row = conn.execute(
                """
                SELECT id, chat_name, status, deadline_at, error, created_at, updated_at
                FROM message_batches
                WHERE chat_name = ? AND status = 'collecting'
                """,
                (normalized_chat_name,),
            ).fetchone()
            if row is None:
                cursor = conn.execute(
                    """
                    INSERT INTO message_batches (
                        chat_name, status, deadline_at, error, created_at, updated_at
                    )
                    VALUES (?, 'collecting', ?, '', ?, ?)
                    """,
                    (normalized_chat_name, float(deadline_at), now, now),
                )
                batch_id = int(cursor.lastrowid)
            else:
                batch_id = int(row["id"])
                conn.execute(
                    """
                    UPDATE message_batches
                    SET deadline_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'collecting'
                    """,
                    (float(deadline_at), now, batch_id),
                )

            position_row = conn.execute(
                """
                SELECT COALESCE(MAX(batch_position), 0) + 1 AS next_position
                FROM messages
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            batch_position = int(position_row["next_position"])
            cursor = conn.execute(
                """
                UPDATE messages
                SET batch_id = ?, batch_position = ?, updated_at = ?
                WHERE id = ? AND batch_id IS NULL
                """,
                (batch_id, batch_position, now, message_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Message could not be appended to the batch.")
            row = conn.execute(
                """
                SELECT id, chat_name, status, deadline_at, error, created_at, updated_at
                FROM message_batches
                WHERE id = ?
                """,
                (batch_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create or load message batch.")
        return self._message_batch_from_row(row)

    def update_batch_deadline(self, batch_id: int, deadline_at: float) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE message_batches
                SET deadline_at = ?, updated_at = ?
                WHERE id = ? AND status = 'collecting'
                """,
                (float(deadline_at), self._now(), batch_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Only a collecting batch deadline can be updated.")

    def get_message_batch(self, batch_id: int) -> MessageBatch | None:
        with self._connect() as conn:
            row = self._select_message_batch(conn, batch_id)
        return self._message_batch_from_row(row) if row else None

    def list_open_batches(self, limit: int | None = 100) -> list[MessageBatch]:
        with self._connect() as conn:
            sql = """
                SELECT id, chat_name, status, deadline_at, error, created_at, updated_at
                FROM message_batches
                WHERE status IN ('collecting', 'processing')
                ORDER BY id ASC
            """
            params: tuple[object, ...] = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (max(1, limit),)
            rows = conn.execute(sql, params).fetchall()
        return [self._message_batch_from_row(row) for row in rows]

    def list_blocked_batches(self, limit: int = 100) -> list[MessageBatch]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, chat_name, status, deadline_at, error, created_at, updated_at
                FROM message_batches
                WHERE status = 'blocked'
                ORDER BY id ASC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [self._message_batch_from_row(row) for row in rows]

    def get_batch_messages(self, batch_id: int) -> list[StoredMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, fingerprint, chat_name, sender, msg_type, content,
                       status, reply, error, received_at, updated_at, batch_id,
                       batch_position, resolved_content
                FROM messages
                WHERE batch_id = ?
                ORDER BY batch_position ASC, id ASC
                """,
                (batch_id,),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def mark_batch_processing(self, batch_id: int, ready_at: float | None = None) -> bool:
        now = self._now()
        with self._connect() as conn:
            deadline_clause = "" if ready_at is None else " AND deadline_at <= ?"
            params: tuple[object, ...] = (
                (now, batch_id)
                if ready_at is None
                else (now, batch_id, float(ready_at))
            )
            cursor = conn.execute(
                f"""
                UPDATE message_batches
                SET status = 'processing', error = '', updated_at = ?
                WHERE id = ? AND status = 'collecting'{deadline_clause}
                """,
                params,
            )
            if cursor.rowcount == 1:
                return True
            row = conn.execute(
                "SELECT status FROM message_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Message batch does not exist.")
            if str(row["status"]) == "processing":
                return False
            if ready_at is not None and str(row["status"]) == "collecting":
                return False
            raise RuntimeError("Message batch state transition is invalid.")

    def mark_batch_completed(self, batch_id: int) -> None:
        self._update_batch_status(batch_id, "completed", allowed_statuses=("processing",))

    def mark_batch_failed(self, batch_id: int, error: str) -> None:
        self._update_batch_status(
            batch_id,
            "failed",
            error=error,
            allowed_statuses=("collecting", "processing"),
        )

    def mark_batch_blocked(self, batch_id: int, error: str) -> None:
        self._update_batch_status(
            batch_id,
            "blocked",
            error=error,
            allowed_statuses=("collecting", "processing"),
        )

    def set_message_resolved_content(self, message_id: int, text: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE messages
                SET resolved_content = ?, updated_at = ?
                WHERE id = ?
                """,
                (text, self._now(), message_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Message does not exist.")

    def upsert_message_media(
        self,
        message_id: int,
        kind: str = "",
        source_path: str = "",
        managed_path: str = "",
        checksum: str = "",
        size_bytes: int = 0,
        status: str | None = None,
        extracted_text: str = "",
        error: str = "",
    ) -> MessageMedia:
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            message = conn.execute(
                "SELECT msg_type FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if message is None:
                raise RuntimeError("Message does not exist.")
            effective_kind = kind or self._media_kind_from_message_type(str(message["msg_type"]))
            if effective_kind not in {"voice", "image"}:
                raise RuntimeError("Media kind must be voice or image.")
            effective_status = status or ("ready" if managed_path else "pending")
            if effective_status not in {"pending", "ready", "processed", "failed"}:
                raise RuntimeError("Media status is invalid.")
            effective_size = int(size_bytes)
            if managed_path and effective_size <= 0:
                try:
                    effective_size = Path(managed_path).stat().st_size
                except OSError:
                    effective_size = 0
            if effective_status == "ready" and (
                not managed_path or not checksum or effective_size <= 0
            ):
                raise RuntimeError("Ready media requires a managed path, checksum, and positive size.")
            conn.execute(
                """
                INSERT INTO message_media (
                    message_id, kind, source_path, managed_path, checksum, size_bytes,
                    status, extracted_text, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    kind = excluded.kind,
                    source_path = excluded.source_path,
                    managed_path = excluded.managed_path,
                    checksum = excluded.checksum,
                    size_bytes = excluded.size_bytes,
                    status = excluded.status,
                    extracted_text = excluded.extracted_text,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    message_id,
                    effective_kind,
                    source_path,
                    managed_path,
                    checksum,
                    effective_size,
                    effective_status,
                    extracted_text,
                    error,
                    now,
                    now,
                ),
            )
            row = self._select_message_media(conn, message_id)
        if row is None:
            raise RuntimeError("Failed to save message media.")
        return self._message_media_from_row(row)

    def get_message_media(self, message_id: int) -> MessageMedia | None:
        with self._connect() as conn:
            row = self._select_message_media(conn, message_id)
        return self._message_media_from_row(row) if row else None

    def mark_message_media_processed(
        self,
        message_id: int,
        extracted_text: str | None = None,
    ) -> None:
        self._update_message_media_status(
            message_id,
            status="processed",
            extracted_text=extracted_text,
            error="",
        )

    def mark_message_media_failed(self, message_id: int, error: str) -> None:
        self._update_message_media_status(message_id, status="failed", error=error)

    def mark_messages_processing(self, message_ids: list[int]) -> None:
        self._update_message_status(
            message_ids,
            "processing",
            allowed_statuses=("received", "processing"),
        )

    def mark_messages_generated(self, message_ids: list[int], reply: str) -> None:
        self._update_message_status(
            message_ids,
            "generated",
            reply=reply,
            allowed_statuses=("processing", "generated"),
        )

    def mark_messages_failed(self, message_ids: list[int], error: str) -> None:
        self._update_message_status(
            message_ids,
            "failed",
            error=error,
            allowed_statuses=("received", "processing", "generated", "failed"),
        )

    def enqueue_outbox(
        self,
        message_ids: list[int],
        chat_name: str,
        sender: str,
        msg_type: str,
        policy: str,
        content: str,
        reply: str,
        kind: str = "reply",
    ) -> OutboxJob:
        normalized_ids = self._normalize_message_ids(message_ids)
        dedupe_key = self._outbox_dedupe_key(normalized_ids, kind)
        now = self._now()
        source_message_ids = json.dumps(normalized_ids, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"SELECT id, status FROM messages WHERE id IN ({self._placeholders(normalized_ids)})",
                normalized_ids,
            ).fetchall()
            if len(rows) != len(normalized_ids):
                raise RuntimeError("All source messages must exist before enqueueing outbox work.")

            cursor = conn.execute(
                """
                INSERT INTO outbox (
                    dedupe_key, source_message_ids, chat_name, sender, msg_type,
                    policy, content_preview, reply, kind, status, attempts,
                    sent_parts, next_attempt_at, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0, 0, '', ?, ?)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (
                    dedupe_key,
                    source_message_ids,
                    chat_name,
                    sender,
                    msg_type,
                    policy,
                    self._preview(content),
                    reply,
                    kind,
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 1:
                invalid_statuses = [
                    str(row["status"])
                    for row in rows
                    if str(row["status"]) not in {"processing", "generated"}
                ]
                if invalid_statuses:
                    raise RuntimeError(
                        "Outbox source messages must be processing or generated before enqueueing."
                    )
                conn.execute(
                    f"""
                    UPDATE messages
                    SET status = 'generated', reply = ?, error = '', updated_at = ?
                    WHERE id IN ({self._placeholders(normalized_ids)})
                    """,
                    (reply, now, *normalized_ids),
                )
            row = conn.execute(
                """
                SELECT id, dedupe_key, source_message_ids, chat_name, sender,
                       msg_type, policy, content_preview, reply, kind, status,
                       attempts, sent_parts, next_attempt_at, error, created_at, updated_at
                FROM outbox
                WHERE dedupe_key = ?
                """,
                (dedupe_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to enqueue outbox work.")
        return self._outbox_from_row(row)

    def list_outbox(
        self,
        status: str = "queued",
        limit: int = 20,
        ready_at: float | None = None,
    ) -> list[OutboxJob]:
        with self._connect() as conn:
            ready_clause = "" if ready_at is None else " AND next_attempt_at <= ?"
            params: tuple[object, ...] = (
                (status, max(1, limit))
                if ready_at is None
                else (status, float(ready_at), max(1, limit))
            )
            rows = conn.execute(
                f"""
                SELECT id, dedupe_key, source_message_ids, chat_name, sender,
                       msg_type, policy, content_preview, reply, kind, status,
                       attempts, sent_parts, next_attempt_at, error, created_at, updated_at
                FROM outbox
                WHERE status = ?{ready_clause}
                ORDER BY id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._outbox_from_row(row) for row in rows]

    def claim_outbox(self, outbox_id: int, ready_at: float | None = None) -> OutboxJob | None:
        now = self._now()
        due_at = time.time() if ready_at is None else float(ready_at)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE outbox
                SET status = 'sending', attempts = attempts + 1, error = '', updated_at = ?
                WHERE id = ? AND status = 'queued' AND next_attempt_at <= ?
                """,
                (now, outbox_id, due_at),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                """
                SELECT id, dedupe_key, source_message_ids, chat_name, sender,
                       msg_type, policy, content_preview, reply, kind, status,
                       attempts, sent_parts, next_attempt_at, error, created_at, updated_at
                FROM outbox
                WHERE id = ?
                """,
                (outbox_id,),
            ).fetchone()
            if row is not None:
                self._update_messages_in_connection(
                    conn,
                    self._decode_message_ids(str(row["source_message_ids"])),
                    status="sending",
                    error="",
                    updated_at=now,
                )
        return self._outbox_from_row(row) if row else None

    def mark_outbox_sent(self, outbox_id: int) -> None:
        self._finish_outbox(outbox_id, status="sent", message_status="sent", error="")

    def mark_outbox_part_sent(self, outbox_id: int, part_index: int) -> None:
        expected_sent_parts = int(part_index)
        if expected_sent_parts < 0:
            raise RuntimeError("part_index must not be negative.")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE outbox
                SET sent_parts = sent_parts + 1, updated_at = ?
                WHERE id = ? AND status = 'sending' AND sent_parts = ?
                """,
                (self._now(), outbox_id, expected_sent_parts),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Outbox part progress could not be persisted.")

    def mark_outbox_uncertain(self, outbox_id: int, error: str) -> None:
        self._finish_outbox(outbox_id, status="uncertain", message_status="failed", error=error)

    def schedule_outbox_retry(
        self,
        outbox_id: int,
        error: str,
        max_attempts: int,
        next_attempt_at: float,
    ) -> bool:
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, attempts, source_message_ids FROM outbox WHERE id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Outbox job does not exist.")
            if str(row["status"]) != "sending":
                raise RuntimeError("Only a sending outbox job can be retried.")
            message_ids = self._decode_message_ids(str(row["source_message_ids"]))
            retry = int(row["attempts"] or 0) < max(1, int(max_attempts))
            status = "queued" if retry else "uncertain"
            message_status = "generated" if retry else "failed"
            conn.execute(
                """
                UPDATE outbox
                SET status = ?, next_attempt_at = ?, error = ?, updated_at = ?
                WHERE id = ? AND status = 'sending'
                """,
                (status, float(next_attempt_at) if retry else 0.0, error, now, outbox_id),
            )
            self._update_messages_in_connection(
                conn,
                message_ids,
                status=message_status,
                error=error,
                updated_at=now,
            )
        return retry

    def recover_interrupted_outbox(
        self,
        retry_enabled: bool = False,
        max_attempts: int = 1,
        next_attempt_at: float = 0.0,
    ) -> int:
        error = "Interrupted while sending; delivery result is uncertain."
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, source_message_ids, attempts FROM outbox WHERE status = 'sending'"
            ).fetchall()
            for row in rows:
                message_ids = self._decode_message_ids(str(row["source_message_ids"]))
                retry = retry_enabled and int(row["attempts"] or 0) < max(1, int(max_attempts))
                status = "queued" if retry else "uncertain"
                message_status = "generated" if retry else "failed"
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = ?, next_attempt_at = ?, error = ?, updated_at = ?
                    WHERE id = ? AND status = 'sending'
                    """,
                    (
                        status,
                        float(next_attempt_at) if retry else 0.0,
                        error,
                        now,
                        int(row["id"]),
                    ),
                )
                self._update_messages_in_connection(
                    conn,
                    message_ids,
                    status=message_status,
                    error=error,
                    updated_at=now,
                )
        return len(rows)

    def requeue_uncertain_outbox(
        self,
        max_attempts: int,
        next_attempt_at: float,
    ) -> int:
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, source_message_ids
                FROM outbox
                WHERE status = 'uncertain' AND attempts < ?
                """,
                (max(1, int(max_attempts)),),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = 'queued', next_attempt_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'uncertain'
                    """,
                    (float(next_attempt_at), now, int(row["id"])),
                )
                self._update_messages_in_connection(
                    conn,
                    self._decode_message_ids(str(row["source_message_ids"])),
                    status="generated",
                    error="retrying uncertain desktop delivery",
                    updated_at=now,
                )
        return len(rows)

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

    def enqueue_failed_reply(
        self,
        chat_name: str,
        reply: str,
        sender: str = "",
        msg_type: str = "",
        policy: str = "",
        content: str = "",
        error: str = "",
    ) -> int:
        now = self._now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO failed_replies (
                    chat_name, reply, sender, msg_type, policy, content_preview,
                    error, attempts, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?)
                """,
                (
                    chat_name,
                    reply,
                    sender,
                    msg_type,
                    policy,
                    self._preview(content),
                    error,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_failed_replies(self, status: str = "pending", limit: int = 20) -> list[FailedReply]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, chat_name, reply, sender, msg_type, policy, content_preview,
                       error, attempts, status, created_at, updated_at
                FROM failed_replies
                WHERE status = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (status, max(1, limit)),
            ).fetchall()
        return [self._failed_reply_from_row(row) for row in rows]

    def mark_failed_reply_sent(self, reply_id: int) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE failed_replies
                SET status = 'sent', updated_at = ?
                WHERE id = ?
                """,
                (now, reply_id),
            )

    def mark_failed_reply_attempt(self, reply_id: int, error: str, max_attempts: int = 3) -> None:
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM failed_replies WHERE id = ?",
                (reply_id,),
            ).fetchone()
            attempts = int(row["attempts"] or 0) + 1 if row else 1
            status = "failed" if attempts >= max_attempts else "pending"
            conn.execute(
                """
                UPDATE failed_replies
                SET attempts = ?, error = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempts, error, status, now, reply_id),
            )

    def _update_batch_status(
        self,
        batch_id: int,
        status: str,
        allowed_statuses: tuple[str, ...],
        error: str = "",
    ) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM message_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Message batch does not exist.")
            current_status = str(row["status"])
            if current_status == status:
                if status in {"blocked", "failed"}:
                    conn.execute(
                        "UPDATE message_batches SET error = ?, updated_at = ? WHERE id = ?",
                        (error, now, batch_id),
                    )
                return
            if current_status in {"completed", "failed"} or current_status not in allowed_statuses:
                raise RuntimeError("Message batch state transition is invalid.")
            conn.execute(
                """
                UPDATE message_batches
                SET status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, now, batch_id),
            )

    def _update_message_media_status(
        self,
        message_id: int,
        status: str,
        extracted_text: str | None = None,
        error: str | None = None,
    ) -> None:
        assignments = ["status = ?", "updated_at = ?"]
        params: list[object] = [status, self._now()]
        if extracted_text is not None:
            assignments.append("extracted_text = ?")
            params.append(extracted_text)
        if error is not None:
            assignments.append("error = ?")
            params.append(error)
        params.append(message_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE message_media SET {', '.join(assignments)} WHERE message_id = ?",
                params,
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Message media does not exist.")

    @staticmethod
    def _select_message_media(
        conn: sqlite3.Connection,
        message_id: int,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT message_id, kind, source_path, managed_path, checksum, size_bytes,
                   status, extracted_text, error, created_at, updated_at
            FROM message_media
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()

    @staticmethod
    def _select_message_batch(
        conn: sqlite3.Connection,
        batch_id: int,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT id, chat_name, status, deadline_at, error, created_at, updated_at
            FROM message_batches
            WHERE id = ?
            """,
            (batch_id,),
        ).fetchone()

    @staticmethod
    def _get_or_create_collecting_batch(
        conn: sqlite3.Connection,
        chat_name: str,
        deadline_at: float,
        now: str,
        update_existing_deadline: bool,
    ) -> int:
        row = conn.execute(
            """
            SELECT id
            FROM message_batches
            WHERE chat_name = ? AND status = 'collecting'
            """,
            (chat_name,),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                """
                INSERT INTO message_batches (
                    chat_name, status, deadline_at, error, created_at, updated_at
                )
                VALUES (?, 'collecting', ?, '', ?, ?)
                """,
                (chat_name, deadline_at, now, now),
            )
            return int(cursor.lastrowid)
        batch_id = int(row["id"])
        if update_existing_deadline:
            conn.execute(
                """
                UPDATE message_batches
                SET deadline_at = ?, updated_at = ?
                WHERE id = ? AND status = 'collecting'
                """,
                (deadline_at, now, batch_id),
            )
        return batch_id

    @staticmethod
    def _next_batch_position(conn: sqlite3.Connection, batch_id: int) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(batch_position), 0) + 1 AS next_position
            FROM messages
            WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        return int(row["next_position"])

    def _update_message_status(
        self,
        message_ids: list[int],
        status: str,
        reply: str | None = None,
        error: str | None = None,
        allowed_statuses: tuple[str, ...] = (),
    ) -> None:
        normalized_ids = self._normalize_message_ids(message_ids)
        now = self._now()
        assignments = ["status = ?", "updated_at = ?"]
        params: list[object] = [status, now]
        if reply is not None:
            assignments.extend(["reply = ?", "error = ''"])
            params.append(reply)
        if error is not None:
            assignments.append("error = ?")
            params.append(error)
        params.extend(normalized_ids)
        status_filter = ""
        if allowed_statuses:
            status_filter = f" AND status IN ({','.join('?' for _ in allowed_statuses)})"
            params.extend(allowed_statuses)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE messages
                SET {', '.join(assignments)}
                WHERE id IN ({self._placeholders(normalized_ids)}){status_filter}
                """,
                params,
            )
            if cursor.rowcount != len(normalized_ids):
                raise RuntimeError("One or more message state transitions are invalid.")

    def _finish_outbox(self, outbox_id: int, status: str, message_status: str, error: str) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, source_message_ids FROM outbox WHERE id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Outbox job does not exist.")
            current_status = str(row["status"])
            if current_status == status:
                return
            if current_status != "sending":
                raise RuntimeError("Only a sending outbox job can be completed.")
            message_ids = self._decode_message_ids(str(row["source_message_ids"]))
            conn.execute(
                """
                UPDATE outbox
                SET status = ?, next_attempt_at = 0, error = ?, updated_at = ?
                WHERE id = ? AND status = 'sending'
                """,
                (status, error, now, outbox_id),
            )
            self._update_messages_in_connection(
                conn,
                message_ids,
                status=message_status,
                error=error,
                updated_at=now,
            )

    @classmethod
    def _update_messages_in_connection(
        cls,
        conn: sqlite3.Connection,
        message_ids: list[int],
        status: str,
        error: str,
        updated_at: str,
    ) -> None:
        if not message_ids:
            return
        conn.execute(
            f"""
            UPDATE messages
            SET status = ?, error = ?, updated_at = ?
            WHERE id IN ({cls._placeholders(message_ids)})
            """,
            (status, error, updated_at, *message_ids),
        )

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
    def _failed_reply_from_row(row: sqlite3.Row) -> FailedReply:
        return FailedReply(
            id=int(row["id"]),
            chat_name=str(row["chat_name"]),
            reply=str(row["reply"]),
            sender=str(row["sender"]),
            msg_type=str(row["msg_type"]),
            policy=str(row["policy"]),
            content_preview=str(row["content_preview"]),
            error=str(row["error"]),
            attempts=int(row["attempts"] or 0),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            id=int(row["id"]),
            fingerprint=str(row["fingerprint"]),
            chat_name=str(row["chat_name"]),
            sender=str(row["sender"]),
            msg_type=str(row["msg_type"]),
            content=str(row["content"]),
            status=str(row["status"]),
            reply=str(row["reply"]),
            error=str(row["error"]),
            received_at=str(row["received_at"]),
            updated_at=str(row["updated_at"]),
            batch_id=int(row["batch_id"]) if row["batch_id"] is not None else None,
            batch_position=(
                int(row["batch_position"]) if row["batch_position"] is not None else None
            ),
            resolved_content=str(row["resolved_content"]),
        )

    @staticmethod
    def _message_batch_from_row(row: sqlite3.Row) -> MessageBatch:
        return MessageBatch(
            id=int(row["id"]),
            chat_name=str(row["chat_name"]),
            status=str(row["status"]),
            deadline_at=float(row["deadline_at"]),
            error=str(row["error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _message_media_from_row(row: sqlite3.Row) -> MessageMedia:
        return MessageMedia(
            message_id=int(row["message_id"]),
            kind=str(row["kind"]),
            source_path=str(row["source_path"]),
            managed_path=str(row["managed_path"]),
            checksum=str(row["checksum"]),
            size_bytes=int(row["size_bytes"] or 0),
            status=str(row["status"]),
            extracted_text=str(row["extracted_text"]),
            error=str(row["error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @classmethod
    def _outbox_from_row(cls, row: sqlite3.Row) -> OutboxJob:
        return OutboxJob(
            id=int(row["id"]),
            dedupe_key=str(row["dedupe_key"]),
            source_message_ids=tuple(cls._decode_message_ids(str(row["source_message_ids"]))),
            chat_name=str(row["chat_name"]),
            sender=str(row["sender"]),
            msg_type=str(row["msg_type"]),
            policy=str(row["policy"]),
            content_preview=str(row["content_preview"]),
            reply=str(row["reply"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            attempts=int(row["attempts"] or 0),
            sent_parts=int(row["sent_parts"] or 0),
            next_attempt_at=float(row["next_attempt_at"] or 0.0),
            error=str(row["error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _normalize_message_ids(message_ids: list[int]) -> list[int]:
        normalized = sorted({int(message_id) for message_id in message_ids})
        if not normalized:
            raise RuntimeError("At least one source message is required.")
        return normalized

    @staticmethod
    def _decode_message_ids(value: str) -> list[int]:
        decoded = json.loads(value)
        if not isinstance(decoded, list) or not all(isinstance(item, int) for item in decoded):
            raise RuntimeError("Outbox source_message_ids is invalid.")
        return decoded

    @staticmethod
    def _outbox_dedupe_key(message_ids: list[int], kind: str) -> str:
        payload = json.dumps(
            {"kind": kind, "source_message_ids": message_ids},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _media_kind_from_message_type(msg_type: str) -> str:
        normalized = msg_type.strip().casefold()
        if normalized in {"voice", "audio"}:
            return "voice"
        if normalized in {"image", "img", "picture", "photo", "pic", "图片", "图像"}:
            return "image"
        return ""

    @staticmethod
    def _placeholders(values: list[int]) -> str:
        return ",".join("?" for _ in values)

    @staticmethod
    def _preview(text: str, limit: int = 200) -> str:
        cleaned = " ".join(str(text).split())
        return cleaned[:limit]

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _ensure_message_batches_blocked_status(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'message_batches'"
        ).fetchone()
        schema = str(row["sql"] or "") if row else ""
        if "'blocked'" in schema:
            return

        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TABLE IF EXISTS message_batches_migration")
            conn.execute(
                """
                CREATE TABLE message_batches_migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'collecting'
                        CHECK(status IN ('collecting', 'processing', 'blocked', 'completed', 'failed')),
                    deadline_at REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO message_batches_migration (
                    id, chat_name, status, deadline_at, error, created_at, updated_at
                )
                SELECT id, chat_name, status, deadline_at, error, created_at, updated_at
                FROM message_batches
                """
            )
            conn.execute("DROP TABLE message_batches")
            conn.execute("ALTER TABLE message_batches_migration RENAME TO message_batches")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
