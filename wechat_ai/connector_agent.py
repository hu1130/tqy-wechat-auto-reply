from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import os
import time
from typing import Any, Callable

from wechat_ai.agent_spool import AgentSpool
from wechat_ai.agent_updater import UpdateError, UpdateStateStore
from wechat_ai.center_agent import (
    AgentHeartbeat,
    CenterAgentClient,
    CenterHTTPError,
    CenterTransportError,
    DeliveryResult,
    DeliveryStart,
    ContactSnapshot,
    ContactSnapshotItem,
    FriendRequestSnapshot,
    FriendRequestSnapshotItem,
    InboundEnvelope,
    OperationResult,
    OperationStart,
)
from wechat_ai.config import AppConfig
from wechat_ai.connectors.protocol import InboundMessage, OutboundMessage
from wechat_ai.connectors.registry import ConnectorDefinition, ConnectorRegistry
from wechat_ai.connectors.wechat_desktop import WeChatDesktopConnector
from wechat_ai.ops import write_health_file
from wechat_ai.reliability import CircuitBreaker, RetryPolicy
from wechat_ai.friend_requests import FriendRequestNotExecutableError
from wechat_ai.wechat_runtime import OperationRejectedError


DEFAULT_CONNECTOR_CHANNEL = "wechat_desktop"
DEFAULT_CONNECTOR_ID = "local-wechat-desktop"
AGENT_INSTALL_ROOT_ENV = "WECHAT_AGENT_INSTALL_ROOT"
AGENT_RUNNING_VERSION_ENV = "WECHAT_AGENT_RUNNING_VERSION"


def packaged_updater_status() -> dict[str, object]:
    install_root = os.getenv(AGENT_INSTALL_ROOT_ENV, "").strip()
    running_version = os.getenv(AGENT_RUNNING_VERSION_ENV, "").strip()
    if not install_root:
        return {}
    try:
        state = UpdateStateStore(install_root).load()
    except (OSError, UpdateError, ValueError):
        result: dict[str, object] = {
            "status": "state_unavailable",
            "failure_code": "update_state_unavailable",
        }
        if running_version:
            result["running_version"] = running_version[:64]
        return result

    result = {"status": state.status}
    if running_version:
        result["running_version"] = running_version[:64]
    for field_name in (
        "active_version",
        "previous_version",
        "target_version",
        "failed_version",
        "checked_at",
        "confirmed_at",
        "rolled_back_at",
        "failed_at",
        "updated_at",
        "failure_code",
        "failure_digest",
    ):
        value = str(getattr(state, field_name, ""))
        if value:
            result[field_name] = value
    return result


def build_wechat_desktop_connector(config: AppConfig, connector_id: str = DEFAULT_CONNECTOR_ID) -> WeChatDesktopConnector:
    scan_all = any(chat.strip() == "*" for chat in config.wechat_listen_chats)
    listen_chats = [] if scan_all else config.wechat_listen_chats
    return WeChatDesktopConnector(
        connector_id=connector_id,
        scan_all_sessions=scan_all,
        listen_chats=listen_chats,
        operation_timeout_seconds=config.wechat_ui_operation_timeout_seconds,
        explicit_client_version=config.wechat_client_version,
        verified_version_prefixes=config.wechat_verified_version_prefixes,
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


class CenterConnectorAgent:
    def __init__(
        self,
        connector: Any,
        client: CenterAgentClient,
        spool: AgentSpool,
        *,
        claim_limit: int = 10,
        heartbeat_interval_seconds: float = 30.0,
        contact_sync_interval_seconds: float = 300.0,
        friend_request_sync_interval_seconds: float = 15.0,
        operation_claim_limit: int = 1,
        runtime_status_provider: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        self.connector = connector
        self.client = client
        self.spool = spool
        self.claim_limit = claim_limit
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.contact_sync_interval_seconds = self._positive_interval(
            contact_sync_interval_seconds,
            "contact_sync_interval_seconds",
        )
        self.friend_request_sync_interval_seconds = self._positive_interval(
            friend_request_sync_interval_seconds,
            "friend_request_sync_interval_seconds",
        )
        if type(operation_claim_limit) is not int or not 1 <= operation_claim_limit <= 100:
            raise ValueError("operation_claim_limit must be an integer between 1 and 100")
        self.operation_claim_limit = operation_claim_limit
        self.runtime_status_provider = (
            runtime_status_provider or packaged_updater_status
        )
        self._last_heartbeat = 0.0
        self._last_contact_sync = 0.0
        self._last_friend_request_sync = 0.0
        self._started = False
        self._operations_allowed = False
        self._identity_status = "unavailable"
        self._verified_wechat_identity: dict[str, str] = {}
        self._identity_seeded = False

    def start(self) -> None:
        if self._started:
            return
        status = self.connector.status()
        recovered_sends = self.spool.recover_interrupted_sends()
        recovered_operations = self.spool.recover_interrupted_operations()
        cleaned = self.spool.cleanup_reported()
        self._started = True
        self._report_heartbeat(status)
        print(
            json.dumps(
                {
                    "event": "center_agent_started",
                    "connector_id": status.connector_id,
                    "online": status.online,
                    "identity_status": self._identity_status,
                    "operations_allowed": self._operations_allowed,
                    "recovered_uncertain": recovered_sends + recovered_operations,
                    "recovered_uncertain_sends": recovered_sends,
                    "recovered_uncertain_operations": recovered_operations,
                    "cleaned_reported": cleaned,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def run_once(self) -> dict[str, int]:
        started_now = not self._started
        if started_now:
            self.start()
        stats = {
            "captured": 0,
            "reported": 0,
            "claimed": 0,
            "completed": 0,
            "contacts_synced": 0,
            "friend_requests_synced": 0,
            "operations_claimed": 0,
            "operations_completed": 0,
            "identity_blocked": 0,
        }

        status = self.connector.status()
        self._block_if_local_identity_changed(status)
        if not started_now and self._heartbeat_due():
            self._report_heartbeat(status)

        if not self._operations_ready(status):
            stats["identity_blocked"] = 1
            return stats

        for message in self.connector.poll():
            self.spool.enqueue_inbound(self._inbound_envelope(message))
            stats["captured"] += 1
        stats["reported"] += self._flush_inbound()
        stats["completed"] += self._flush_delivery_results()
        stats["operations_completed"] += self._flush_operation_results()

        stats["contacts_synced"] += self._maybe_sync_contacts()
        stats["friend_requests_synced"] += self._maybe_sync_friend_requests()
        try:
            commands = self.client.claim_deliveries(self.claim_limit)
        except RuntimeError as exc:
            self._event("delivery_claim_failed", error=str(exc))
        else:
            for command in commands:
                self.spool.enqueue_delivery(command)
            stats["claimed"] += len(commands)
            stats["completed"] += self._process_claimed_deliveries()
        try:
            operations = self.client.claim_operations(self.operation_claim_limit)
        except (AttributeError, RuntimeError) as exc:
            if not isinstance(exc, AttributeError):
                self._event("operation_claim_failed", error=str(exc))
        else:
            for operation in operations:
                self.spool.enqueue_operation(operation)
            stats["operations_claimed"] += len(operations)
            stats["operations_completed"] += self._process_claimed_operations()
        return stats

    def _report_heartbeat(self, status: Any) -> None:
        try:
            heartbeat = AgentHeartbeat(
                tenant_id=self.client.tenant_id,
                agent_id=self.client.agent_id,
                channel_account_id=self.client.channel_account_id,
                status="online" if status.online else "degraded",
                observed_at=datetime.now(timezone.utc).isoformat(),
                wechat_identity=self._wechat_identity(status),
                capabilities=self._heartbeat_capabilities(status),
            )
            response = self.client.heartbeat(heartbeat)
            self._apply_heartbeat_response(response)
        except Exception as exc:
            self._block_operations("unavailable")
            self._last_heartbeat = 0.0
            self._event("heartbeat_failed", error=str(exc))
        else:
            self._last_heartbeat = time.monotonic()

    def _heartbeat_due(self) -> bool:
        if not self._operations_allowed:
            return True
        return time.monotonic() - self._last_heartbeat >= self.heartbeat_interval_seconds

    def _heartbeat_capabilities(self, status: Any) -> dict[str, object]:
        result: dict[str, object] = {}
        details = getattr(status, "details", {})
        if isinstance(details, Mapping):
            connector = details.get("capabilities")
            if isinstance(connector, Mapping):
                result["connector"] = {
                    str(name): bool(value)
                    for name, value in connector.items()
                    if type(name) is str
                }
        try:
            updater = self.runtime_status_provider()
        except Exception as exc:
            self._event("updater_status_read_failed", error=type(exc).__name__)
        else:
            if isinstance(updater, Mapping) and updater:
                result["updater"] = dict(updater)
        return result

    @staticmethod
    def _wechat_identity(status: Any) -> dict[str, str]:
        details = getattr(status, "details", {})
        if not isinstance(details, Mapping):
            return {}
        identity = details.get("wechat_identity", {})
        if not isinstance(identity, Mapping):
            return {}
        return dict(identity)

    @staticmethod
    def _wechat_identity_key(identity: Mapping[str, str]) -> tuple[str, str] | None:
        account_id = str(identity.get("account_id", "")).strip()
        if account_id:
            return ("account_id", account_id)
        nickname = str(identity.get("nickname", "")).strip()
        if nickname:
            return ("nickname", nickname)
        return None

    def _apply_heartbeat_response(self, response: object) -> None:
        if not isinstance(response, Mapping):
            raise RuntimeError("heartbeat response must be a JSON object")
        if response.get("accepted") is not True:
            raise RuntimeError("heartbeat response was not accepted")
        identity_status = response.get("identity_status")
        operations_allowed = response.get("operations_allowed")
        if identity_status not in {"unavailable", "pending", "verified", "mismatch"}:
            raise RuntimeError("heartbeat response has an invalid identity_status")
        if type(operations_allowed) is not bool:
            raise RuntimeError("heartbeat response has an invalid operations_allowed value")
        verified_identity = AgentHeartbeat(
            tenant_id=self.client.tenant_id,
            agent_id=self.client.agent_id,
            channel_account_id=self.client.channel_account_id,
            status="online",
            observed_at=datetime.now(timezone.utc).isoformat(),
            wechat_identity=response.get("wechat_identity", {}),
        ).wechat_identity
        allowed = identity_status == "verified" and operations_allowed
        if allowed and not verified_identity:
            raise RuntimeError("verified heartbeat response is missing WeChat identity")
        if not allowed:
            self._block_operations(str(identity_status))
            return
        if self._wechat_identity_key(verified_identity) != self._wechat_identity_key(
            self._verified_wechat_identity
        ):
            self._identity_seeded = False
        self._verified_wechat_identity = dict(verified_identity)
        self._identity_status = "verified"
        self._operations_allowed = True

    def _block_operations(self, identity_status: str) -> None:
        was_allowed = self._operations_allowed
        self._operations_allowed = False
        self._identity_status = identity_status
        self._identity_seeded = False
        if was_allowed:
            self._event("identity_operations_blocked", identity_status=identity_status)

    def _block_if_local_identity_changed(self, status: Any) -> None:
        if not self._operations_allowed:
            return
        current_identity = self._wechat_identity(status)
        if self._wechat_identity_key(current_identity) == self._wechat_identity_key(
            self._verified_wechat_identity
        ):
            return
        identity_status = "mismatch" if current_identity else "unavailable"
        self._block_operations(identity_status)
        self._last_heartbeat = 0.0

    def _operations_ready(self, status: Any) -> bool:
        if not self._operations_allowed or not bool(getattr(status, "online", False)):
            return False
        if self._wechat_identity_key(self._wechat_identity(status)) != self._wechat_identity_key(
            self._verified_wechat_identity
        ):
            self._block_if_local_identity_changed(status)
            return False
        if self._identity_seeded:
            return True
        try:
            self.connector.seed()
        except Exception as exc:
            self._block_operations("unavailable")
            self._last_heartbeat = 0.0
            self._event("identity_baseline_failed", error=str(exc))
            return False
        self._identity_seeded = True
        self._event("identity_baseline_seeded", identity_status=self._identity_status)
        return True

    def _flush_inbound(self) -> int:
        reported = 0
        for envelope in self.spool.pending_inbound():
            try:
                self.client.report_inbound(envelope)
            except RuntimeError as exc:
                self.spool.mark_inbound_failed(envelope.external_message_id, str(exc))
                self._event("inbound_report_failed", message_id=envelope.external_message_id)
                break
            self.spool.mark_inbound_reported(envelope.external_message_id)
            reported += 1
        return reported

    def _process_claimed_deliveries(self) -> int:
        completed = 0
        for item in self.spool.pending_deliveries():
            if item.status != "claimed":
                continue
            command = item.command
            start = DeliveryStart(
                tenant_id=command.tenant_id,
                agent_id=command.agent_id,
                channel_account_id=command.channel_account_id,
                delivery_id=command.delivery_id,
                lease_token=command.lease_token,
                attempt=command.attempt,
            )
            self.spool.mark_delivery_starting(
                command.delivery_id,
                command.lease_token,
            )
            try:
                send_allowed = self.client.start_delivery(start)
            except RuntimeError as exc:
                self._event("delivery_start_failed", delivery_id=command.delivery_id, error=str(exc))
                continue
            if send_allowed is False:
                self.spool.mark_delivery_suppressed(
                    command.delivery_id,
                    command.lease_token,
                )
                continue

            self.spool.mark_delivery_sending(command.delivery_id, command.lease_token)
            try:
                self.connector.send(
                    OutboundMessage(
                        connector_id=self.connector.connector_id,
                        channel=self.connector.channel,
                        conversation_id=command.conversation_external_id,
                        content=command.content,
                    )
                )
            except Exception as exc:
                self.spool.mark_delivery_result(
                    command.delivery_id,
                    command.lease_token,
                    "uncertain",
                    error=f"WeChat send raised after start: {exc}",
                )
            else:
                self.spool.mark_delivery_result(
                    command.delivery_id,
                    command.lease_token,
                    "sent",
                )
            completed += self._flush_delivery_results()
        return completed

    def _flush_delivery_results(self) -> int:
        reported = 0
        for item in self.spool.pending_deliveries():
            if item.status not in {"sent", "failed", "uncertain"}:
                continue
            result = DeliveryResult(
                tenant_id=item.command.tenant_id,
                agent_id=item.command.agent_id,
                channel_account_id=item.command.channel_account_id,
                delivery_id=item.command.delivery_id,
                lease_token=item.command.lease_token,
                attempt=item.command.attempt,
                status=item.status,
                completed_at=item.completed_at,
                error=item.result_error or None,
            )
            try:
                self.client.complete_delivery(result)
            except RuntimeError as exc:
                self._event(
                    "delivery_result_failed",
                    delivery_id=item.command.delivery_id,
                    error=str(exc),
                )
                continue
            self.spool.mark_delivery_reported(
                item.command.delivery_id,
                item.command.lease_token,
            )
            reported += 1
        return reported

    def _maybe_sync_contacts(self) -> int:
        snapshotter = getattr(self.connector, "snapshot_contacts", None)
        sync = getattr(self.client, "sync_contacts", None)
        if not callable(snapshotter) or not callable(sync):
            return 0
        now = time.monotonic()
        if now - self._last_contact_sync < self.contact_sync_interval_seconds:
            return 0
        self._last_contact_sync = now
        observed_at = datetime.now(timezone.utc).isoformat()
        try:
            raw_contacts = list(snapshotter())
            contacts = [ContactSnapshotItem.from_dict(item) for item in raw_contacts]
            snapshot = ContactSnapshot(
                tenant_id=self.client.tenant_id,
                agent_id=self.client.agent_id,
                channel_account_id=self.client.channel_account_id,
                snapshot_id=self._snapshot_id("contacts", observed_at, raw_contacts),
                observed_at=observed_at,
                contacts=contacts,
            )
            sync(snapshot)
        except Exception as exc:
            self._event("contact_sync_failed", error=str(exc))
            return 0
        return len(contacts)

    def _maybe_sync_friend_requests(self) -> int:
        snapshotter = getattr(self.connector, "snapshot_friend_requests", None)
        sync = getattr(self.client, "sync_friend_requests", None)
        if not callable(snapshotter) or not callable(sync):
            return 0
        now = time.monotonic()
        if now - self._last_friend_request_sync < self.friend_request_sync_interval_seconds:
            return 0
        self._last_friend_request_sync = now
        observed_at = datetime.now(timezone.utc).isoformat()
        try:
            raw_requests = list(snapshotter())
            normalized = []
            for item in raw_requests:
                value = dict(item)
                value.setdefault("requested_at", observed_at)
                normalized.append(value)
            requests = [FriendRequestSnapshotItem.from_dict(item) for item in normalized]
            snapshot = FriendRequestSnapshot(
                tenant_id=self.client.tenant_id,
                agent_id=self.client.agent_id,
                channel_account_id=self.client.channel_account_id,
                snapshot_id=self._snapshot_id("friend-requests", observed_at, normalized),
                observed_at=observed_at,
                requests=requests,
            )
            sync(snapshot)
        except Exception as exc:
            self._event("friend_request_sync_failed", error=str(exc))
            return 0
        return len(requests)

    def _process_claimed_operations(self) -> int:
        completed = 0
        for item in self.spool.pending_operations():
            if item.status != "claimed":
                continue
            command = item.command
            start = OperationStart(
                tenant_id=command.tenant_id,
                agent_id=command.agent_id,
                channel_account_id=command.channel_account_id,
                operation_id=command.operation_id,
                lease_token=command.lease_token,
                attempt=command.attempt,
            )
            self.spool.mark_operation_starting(
                command.operation_id,
                command.lease_token,
            )
            try:
                self.client.start_operation(start)
            except RuntimeError as exc:
                self._event(
                    "operation_start_failed",
                    operation_id=command.operation_id,
                    error=str(exc),
                )
                continue

            self.spool.mark_operation_executing(
                command.operation_id,
                command.lease_token,
            )
            try:
                result = self.connector.execute_operation(
                    command.operation_type,
                    dict(command.payload),
                )
            except (FriendRequestNotExecutableError, OperationRejectedError, ValueError) as exc:
                self.spool.mark_operation_result(
                    command.operation_id,
                    command.lease_token,
                    "failed",
                    error=f"operation was not attempted: {exc}",
                )
            except Exception as exc:
                self.spool.mark_operation_result(
                    command.operation_id,
                    command.lease_token,
                    "uncertain",
                    error=f"WeChat operation raised after start: {exc}",
                )
            else:
                evidence = result.get("evidence", {}) if isinstance(result, dict) else {}
                if not isinstance(evidence, dict):
                    evidence = {"raw_result": str(evidence)}
                self.spool.mark_operation_result(
                    command.operation_id,
                    command.lease_token,
                    "succeeded",
                    evidence=evidence,
                )
            completed += self._flush_operation_results()
        return completed

    def _flush_operation_results(self) -> int:
        reported = 0
        for item in self.spool.pending_operations():
            if item.status not in {"succeeded", "failed", "uncertain"}:
                continue
            result = OperationResult(
                tenant_id=item.command.tenant_id,
                agent_id=item.command.agent_id,
                channel_account_id=item.command.channel_account_id,
                operation_id=item.command.operation_id,
                lease_token=item.command.lease_token,
                attempt=item.command.attempt,
                status=item.status,
                completed_at=item.completed_at,
                error=item.result_error or None,
                evidence=item.result_evidence,
            )
            try:
                self.client.complete_operation(result)
            except RuntimeError as exc:
                self._event(
                    "operation_result_failed",
                    operation_id=item.command.operation_id,
                    error=str(exc),
                )
                continue
            self.spool.mark_operation_reported(
                item.command.operation_id,
                item.command.lease_token,
            )
            reported += 1
        return reported

    def _inbound_envelope(self, message: InboundMessage) -> InboundEnvelope:
        message_id = message.message_id.strip() or _fallback_message_id(message)
        occurred_at = _message_timestamp(message)
        return InboundEnvelope(
            tenant_id=self.client.tenant_id,
            agent_id=self.client.agent_id,
            channel_account_id=self.client.channel_account_id,
            external_message_id=message_id,
            conversation_external_id=message.conversation_id,
            sender_external_id=message.sender,
            message_type=message.message_type,
            content=message.content,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _snapshot_id(kind: str, observed_at: str, items: list[dict[str, Any]]) -> str:
        canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"{observed_at}\x1f{canonical}".encode("utf-8")).hexdigest()
        return f"{kind}:{digest}"

    @staticmethod
    def _positive_interval(value: float, field_name: str) -> float:
        converted = float(value)
        if converted <= 0:
            raise ValueError(f"{field_name} must be positive")
        return converted

    @staticmethod
    def _event(event: str, **values: object) -> None:
        print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def run_center_connector_agent(
    config: AppConfig,
    connector_id: str = DEFAULT_CONNECTOR_ID,
    channel: str = DEFAULT_CONNECTOR_CHANNEL,
    interval_seconds: float = 1.0,
) -> None:
    if not all(
        (
            config.agent_center_url,
            config.agent_tenant_id,
            config.agent_instance_id,
            config.agent_channel_account_id,
            config.agent_token,
            config.agent_spool_file,
        )
    ):
        raise RuntimeError("Agent center binding and AGENT_SPOOL_FILE must be configured")
    connector = build_connector(config, channel=channel, connector_id=connector_id)
    client = CenterAgentClient(
        config.agent_center_url,
        config.agent_token,
        config.agent_tenant_id,
        config.agent_instance_id,
        config.agent_channel_account_id,
        timeout=config.request_timeout,
        retry_policy=RetryPolicy(
            max_attempts=config.api_retry_attempts,
            base_delay=config.api_retry_base_delay_seconds,
            max_delay=config.api_retry_max_delay_seconds,
            should_retry=lambda error: isinstance(error, CenterTransportError)
            or (
                isinstance(error, CenterHTTPError)
                and (error.status == 429 or error.status >= 500)
            ),
        ),
        circuit_breaker=CircuitBreaker(
            failure_threshold=config.api_circuit_failure_threshold,
            recovery_timeout=config.api_circuit_recovery_seconds,
        ),
    )
    spool = AgentSpool(config.agent_spool_file)
    agent = CenterConnectorAgent(connector, client, spool)
    try:
        agent.start()
        _write_center_agent_health(config, "running", "center Agent started")
        while True:
            try:
                stats = agent.run_once()
            except Exception as exc:
                agent._event("center_agent_iteration_failed", error=str(exc))
                _write_center_agent_health(config, "degraded", str(exc))
            else:
                _write_center_agent_health(
                    config,
                    "running",
                    "center Agent loop completed",
                    stats=stats,
                )
            time.sleep(interval_seconds)
    finally:
        spool.close()


def _write_center_agent_health(
    config: AppConfig,
    status: str,
    message: str,
    *,
    stats: dict[str, int] | None = None,
) -> None:
    if not config.health_file:
        return
    try:
        write_health_file(
            config.health_file,
            {
                "component": "center_agent",
                "status": status,
                "message": message[:1000],
                "tenant_id": config.agent_tenant_id,
                "agent_id": config.agent_instance_id,
                "channel_account_id": config.agent_channel_account_id,
                "stats": dict(stats or {}),
            },
        )
    except Exception as exc:
        print(
            json.dumps(
                {"event": "center_agent_health_write_failed", "error": str(exc)},
                ensure_ascii=False,
            ),
            flush=True,
        )


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


def _message_timestamp(message: InboundMessage) -> str:
    value = message.raw.get("received_at")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return datetime.now(timezone.utc).isoformat()


def _fallback_message_id(message: InboundMessage) -> str:
    source = "\x1f".join(
        (
            message.connector_id,
            message.channel,
            message.conversation_id,
            message.sender,
            message.message_type,
            message.content,
            str(message.raw.get("received_at", "")),
        )
    )
    return f"sha256:{hashlib.sha256(source.encode('utf-8')).hexdigest()}"
