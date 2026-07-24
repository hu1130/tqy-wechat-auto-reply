from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Mapping

from wechat_ai.center_agent import DeliveryCommand, InboundEnvelope, OperationCommand


def _utc_text() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SpoolDelivery:
    command: DeliveryCommand
    status: str
    result_error: str
    completed_at: str


@dataclass(frozen=True)
class SpoolOperation:
    command: OperationCommand
    status: str
    result_error: str
    result_evidence: dict[str, object]
    completed_at: str


class AgentSpool:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbound_spool (
                    idempotency_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'reported')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_inbound_spool_pending
                    ON inbound_spool(status, created_at);

                CREATE TABLE IF NOT EXISTS delivery_spool (
                    delivery_id TEXT PRIMARY KEY,
                    attempt INTEGER NOT NULL,
                    lease_token TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('claimed', 'sending', 'sent', 'failed', 'uncertain', 'reported')
                    ),
                    result_error TEXT NOT NULL DEFAULT '',
                    start_intent_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_delivery_spool_status
                    ON delivery_spool(status, created_at);

                CREATE TABLE IF NOT EXISTS operation_spool (
                    operation_id TEXT PRIMARY KEY,
                    attempt INTEGER NOT NULL,
                    lease_token TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('claimed', 'executing', 'succeeded', 'failed', 'uncertain', 'reported')
                    ),
                    result_error TEXT NOT NULL DEFAULT '',
                    result_evidence_json TEXT NOT NULL DEFAULT '{}',
                    start_intent_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_operation_spool_status
                    ON operation_spool(status, created_at);
                """
            )
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(operation_spool)"
                ).fetchall()
            }
            if "start_intent_at" not in columns:
                self._connection.execute(
                    "ALTER TABLE operation_spool "
                    "ADD COLUMN start_intent_at TEXT NOT NULL DEFAULT ''"
                )
            delivery_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(delivery_spool)"
                ).fetchall()
            }
            if "start_intent_at" not in delivery_columns:
                self._connection.execute(
                    "ALTER TABLE delivery_spool "
                    "ADD COLUMN start_intent_at TEXT NOT NULL DEFAULT ''"
                )

    def enqueue_inbound(self, envelope: InboundEnvelope) -> None:
        now = _utc_text()
        payload = json.dumps(envelope.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO inbound_spool
                    (idempotency_key, payload_json, status, created_at, updated_at)
                VALUES (?, ?, 'pending', ?, ?)
                """,
                (envelope.external_message_id, payload, now, now),
            )

    def pending_inbound(self, limit: int = 100) -> list[InboundEnvelope]:
        rows = self._select(
            """
            SELECT payload_json FROM inbound_spool
            WHERE status = 'pending'
            ORDER BY created_at, idempotency_key
            LIMIT ?
            """,
            (limit,),
        )
        return [InboundEnvelope.from_dict(json.loads(row["payload_json"])) for row in rows]

    def mark_inbound_reported(self, idempotency_key: str) -> None:
        self._update_inbound(idempotency_key, "reported", "")

    def mark_inbound_failed(self, idempotency_key: str, error: str) -> None:
        self._update_inbound(idempotency_key, "pending", error)

    def _update_inbound(self, idempotency_key: str, status: str, error: str) -> None:
        with self._lock, self._connection:
            result = self._connection.execute(
                """
                UPDATE inbound_spool
                SET status = ?, attempts = attempts + 1, last_error = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (status, error[:1000], _utc_text(), idempotency_key),
            )
            if result.rowcount != 1:
                raise LookupError("inbound spool item not found")

    def enqueue_delivery(self, command: DeliveryCommand) -> None:
        now = _utc_text()
        payload = json.dumps(command.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO delivery_spool
                    (delivery_id, attempt, lease_token, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'claimed', ?, ?)
                ON CONFLICT(delivery_id) DO UPDATE SET
                    attempt = excluded.attempt,
                    lease_token = excluded.lease_token,
                    payload_json = excluded.payload_json,
                    status = 'claimed',
                    result_error = '',
                    start_intent_at = '',
                    completed_at = '',
                    updated_at = excluded.updated_at
                WHERE excluded.attempt > delivery_spool.attempt
                """,
                (
                    command.delivery_id,
                    command.attempt,
                    command.lease_token,
                    payload,
                    now,
                    now,
                ),
            )

    def pending_deliveries(self, limit: int = 100) -> list[SpoolDelivery]:
        rows = self._select(
            """
            SELECT payload_json, status, result_error, completed_at
            FROM delivery_spool
            WHERE status != 'reported'
            ORDER BY created_at, delivery_id
            LIMIT ?
            """,
            (limit,),
        )
        return [
            SpoolDelivery(
                command=DeliveryCommand.from_dict(json.loads(row["payload_json"])),
                status=str(row["status"]),
                result_error=str(row["result_error"]),
                completed_at=str(row["completed_at"]),
            )
            for row in rows
        ]

    def mark_delivery_starting(self, delivery_id: str, lease_token: str) -> None:
        with self._lock, self._connection:
            result = self._connection.execute(
                """
                UPDATE delivery_spool
                SET start_intent_at = ?, updated_at = ?
                WHERE delivery_id = ? AND lease_token = ? AND status = 'claimed'
                """,
                (_utc_text(), _utc_text(), delivery_id, lease_token),
            )
            if result.rowcount != 1:
                raise LookupError("claimed delivery spool item or lease token not found")

    def mark_delivery_sending(self, delivery_id: str, lease_token: str) -> None:
        self._update_delivery(
            delivery_id,
            lease_token,
            "sending",
            expected_statuses=("claimed",),
            require_start_intent=True,
        )

    def mark_delivery_suppressed(self, delivery_id: str, lease_token: str) -> None:
        self._update_delivery(
            delivery_id,
            lease_token,
            "reported",
            expected_statuses=("claimed",),
            require_start_intent=True,
        )

    def mark_delivery_result(
        self,
        delivery_id: str,
        lease_token: str,
        status: str,
        *,
        error: str = "",
        completed_at: str | None = None,
    ) -> None:
        if status not in {"sent", "failed", "uncertain"}:
            raise ValueError("delivery result status must be sent, failed, or uncertain")
        if status == "sent" and error:
            raise ValueError("sent delivery cannot include an error")
        if status != "sent" and not error.strip():
            raise ValueError(f"{status} delivery requires an error")
        self._update_delivery(
            delivery_id,
            lease_token,
            status,
            error=error,
            completed_at=completed_at or _utc_text(),
            expected_statuses=("sending",),
        )

    def mark_delivery_reported(self, delivery_id: str, lease_token: str) -> None:
        self._update_delivery(
            delivery_id,
            lease_token,
            "reported",
            expected_statuses=("sent", "failed", "uncertain"),
        )

    def recover_interrupted_sends(self) -> int:
        now = _utc_text()
        error = "Agent restarted while delivery status was sending; actual WeChat result is unknown"
        with self._lock, self._connection:
            result = self._connection.execute(
                """
                UPDATE delivery_spool
                SET status = 'uncertain', result_error = ?, completed_at = ?, updated_at = ?
                WHERE status = 'sending'
                """,
                (error, now, now),
            )
            return result.rowcount

    def enqueue_operation(self, command: OperationCommand) -> None:
        now = _utc_text()
        payload = json.dumps(command.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO operation_spool
                    (operation_id, attempt, lease_token, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'claimed', ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    attempt = excluded.attempt,
                    lease_token = excluded.lease_token,
                    payload_json = excluded.payload_json,
                    status = 'claimed',
                    result_error = '',
                    result_evidence_json = '{}',
                    start_intent_at = '',
                    completed_at = '',
                    updated_at = excluded.updated_at
                WHERE excluded.attempt > operation_spool.attempt
                """,
                (
                    command.operation_id,
                    command.attempt,
                    command.lease_token,
                    payload,
                    now,
                    now,
                ),
            )

    def pending_operations(self, limit: int = 100) -> list[SpoolOperation]:
        rows = self._select(
            """
            SELECT payload_json, status, result_error, result_evidence_json, completed_at
            FROM operation_spool
            WHERE status != 'reported'
            ORDER BY created_at, operation_id
            LIMIT ?
            """,
            (limit,),
        )
        return [
            SpoolOperation(
                command=OperationCommand.from_dict(json.loads(row["payload_json"])),
                status=str(row["status"]),
                result_error=str(row["result_error"]),
                result_evidence=dict(json.loads(row["result_evidence_json"])),
                completed_at=str(row["completed_at"]),
            )
            for row in rows
        ]

    def mark_operation_starting(self, operation_id: str, lease_token: str) -> None:
        with self._lock, self._connection:
            result = self._connection.execute(
                """
                UPDATE operation_spool
                SET start_intent_at = ?, updated_at = ?
                WHERE operation_id = ? AND lease_token = ? AND status = 'claimed'
                """,
                (_utc_text(), _utc_text(), operation_id, lease_token),
            )
            if result.rowcount != 1:
                raise LookupError("claimed operation spool item or lease token not found")

    def mark_operation_executing(self, operation_id: str, lease_token: str) -> None:
        self._update_operation(
            operation_id,
            lease_token,
            "executing",
            expected_statuses=("claimed",),
            require_start_intent=True,
        )

    def mark_operation_result(
        self,
        operation_id: str,
        lease_token: str,
        status: str,
        *,
        error: str = "",
        evidence: Mapping[str, object] | None = None,
        completed_at: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("operation result status must be succeeded, failed, or uncertain")
        if status == "succeeded" and error:
            raise ValueError("succeeded operation cannot include an error")
        if status != "succeeded" and not error.strip():
            raise ValueError(f"{status} operation requires an error")
        self._update_operation(
            operation_id,
            lease_token,
            status,
            error=error,
            evidence=evidence or {},
            completed_at=completed_at or _utc_text(),
            expected_statuses=("executing",),
        )

    def mark_operation_reported(self, operation_id: str, lease_token: str) -> None:
        self._update_operation(
            operation_id,
            lease_token,
            "reported",
            expected_statuses=("succeeded", "failed", "uncertain"),
        )

    def recover_interrupted_operations(self) -> int:
        now = _utc_text()
        error = "Agent restarted while operation was executing; actual WeChat result is unknown"
        with self._lock, self._connection:
            result = self._connection.execute(
                """
                UPDATE operation_spool
                SET status = 'uncertain', result_error = ?, completed_at = ?, updated_at = ?
                WHERE status = 'executing'
                """,
                (error, now, now),
            )
            return result.rowcount

    def cleanup_reported(self, retention_days: int = 7) -> int:
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock, self._connection:
            inbound = self._connection.execute(
                "DELETE FROM inbound_spool WHERE status = 'reported' AND updated_at < ?",
                (cutoff,),
            ).rowcount
            deliveries = self._connection.execute(
                "DELETE FROM delivery_spool WHERE status = 'reported' AND updated_at < ?",
                (cutoff,),
            ).rowcount
            operations = self._connection.execute(
                "DELETE FROM operation_spool WHERE status = 'reported' AND updated_at < ?",
                (cutoff,),
            ).rowcount
        return inbound + deliveries + operations

    def _update_delivery(
        self,
        delivery_id: str,
        lease_token: str,
        status: str,
        *,
        error: str = "",
        completed_at: str = "",
        expected_statuses: tuple[str, ...] | None = None,
        require_start_intent: bool = False,
    ) -> None:
        status_clause = ""
        parameters: list[object] = [
            status,
            error[:1000],
            completed_at,
            _utc_text(),
            delivery_id,
            lease_token,
        ]
        if expected_statuses:
            placeholders = ", ".join("?" for _ in expected_statuses)
            status_clause = f" AND status IN ({placeholders})"
            parameters.extend(expected_statuses)
        if require_start_intent:
            status_clause += " AND start_intent_at != ''"
        with self._lock, self._connection:
            result = self._connection.execute(
                f"""
                UPDATE delivery_spool
                SET status = ?, result_error = ?, completed_at = ?, updated_at = ?
                WHERE delivery_id = ? AND lease_token = ?
                    {status_clause}
                """,
                tuple(parameters),
            )
            if result.rowcount != 1:
                raise LookupError("delivery spool item or lease token not found")

    def _update_operation(
        self,
        operation_id: str,
        lease_token: str,
        status: str,
        *,
        error: str = "",
        evidence: Mapping[str, object] | None = None,
        completed_at: str = "",
        expected_statuses: tuple[str, ...] | None = None,
        require_start_intent: bool = False,
    ) -> None:
        evidence_json = json.dumps(
            dict(evidence or {}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        status_clause = ""
        parameters: list[object] = [
            status,
            error[:1000],
            evidence_json,
            completed_at,
            _utc_text(),
            operation_id,
            lease_token,
        ]
        if expected_statuses:
            placeholders = ", ".join("?" for _ in expected_statuses)
            status_clause = f" AND status IN ({placeholders})"
            parameters.extend(expected_statuses)
        if require_start_intent:
            status_clause += " AND start_intent_at != ''"
        with self._lock, self._connection:
            result = self._connection.execute(
                f"""
                UPDATE operation_spool
                SET status = ?, result_error = ?, result_evidence_json = ?,
                    completed_at = ?, updated_at = ?
                WHERE operation_id = ? AND lease_token = ?
                    {status_clause}
                """,
                tuple(parameters),
            )
            if result.rowcount != 1:
                raise LookupError("operation spool item or lease token not found")

    def _select(self, sql: str, parameters: tuple[object, ...]) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(sql, parameters).fetchall())


__all__ = ["AgentSpool", "SpoolDelivery", "SpoolOperation"]
