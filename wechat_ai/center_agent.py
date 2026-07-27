from __future__ import annotations

import hashlib
import json
import math
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener

from wechat_ai.reliability import CircuitBreaker, RetryPolicy


HEARTBEAT_STATUSES = frozenset({"online", "degraded", "offline"})
HEARTBEAT_IDENTITY_STATUSES = frozenset(
    {"unavailable", "pending", "verified", "mismatch"}
)
WECHAT_IDENTITY_FIELDS = frozenset({"account_id", "nickname"})
DELIVERY_COMMAND_STATUSES = frozenset({"queued", "claimed"})
DELIVERY_RESULT_STATUSES = frozenset({"sent", "failed", "uncertain"})
OPERATION_RESULT_STATUSES = frozenset({"succeeded", "failed", "uncertain"})
CONTACT_KINDS = frozenset({"friend", "group"})
CONTACT_STATUSES = frozenset({"active", "inactive", "deleted"})
FRIEND_REQUEST_STATUSES = frozenset({"pending", "approved", "rejected", "expired"})
OPERATION_TYPES = frozenset({"accept_friend", "reject_friend", "send_welcome"})
MAX_ERROR_TEXT = 512
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ID_TEXT = 256
MAX_NAME_TEXT = 256
MAX_WECHAT_IDENTITY_TEXT = 256
MAX_REMARK_TEXT = 1024
MAX_VERIFICATION_TEXT = 4096
MAX_TIMESTAMP_TEXT = 64
MAX_SNAPSHOT_ITEMS = 5000
MAX_JSON_DEPTH = 8
MAX_JSON_LIST_ITEMS = 1000
MAX_JSON_OBJECT_KEYS = 256
MAX_JSON_KEY_TEXT = 128
MAX_JSON_STRING_TEXT = 16384
MAX_JSON_OBJECT_BYTES = 64 * 1024


class CenterClientError(RuntimeError):
    pass


class CenterTransportError(CenterClientError):
    pass


class CenterHTTPError(CenterClientError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _retryable_center_error(error: BaseException) -> bool:
    return isinstance(error, CenterTransportError) or (
        isinstance(error, CenterHTTPError) and (error.status == 429 or error.status >= 500)
    )


def _text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{field_name} contains an invalid control character")
    return value


def _content(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _limited_text(
    value: object,
    field_name: str,
    max_length: int,
    *,
    allow_empty: bool = False,
    allow_multiline: bool = False,
) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if "\x00" in value or (not allow_multiline and ("\r" in value or "\n" in value)):
        raise ValueError(f"{field_name} contains an invalid control character")
    if len(value) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    return value


def _json_value(value: object, field_name: str, depth: int = 0) -> object:
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"{field_name} exceeds the maximum JSON nesting depth")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{field_name} contains a non-finite number")
        return value
    if type(value) is str:
        return _limited_text(
            value,
            field_name,
            MAX_JSON_STRING_TEXT,
            allow_empty=True,
            allow_multiline=True,
        )
    if type(value) is list:
        if len(value) > MAX_JSON_LIST_ITEMS:
            raise ValueError(
                f"{field_name} must not contain more than {MAX_JSON_LIST_ITEMS} list items"
            )
        return [
            _json_value(item, f"{field_name}[{index}]", depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        if len(value) > MAX_JSON_OBJECT_KEYS:
            raise ValueError(
                f"{field_name} must not contain more than {MAX_JSON_OBJECT_KEYS} object keys"
            )
        result: dict[str, object] = {}
        for key, item in value.items():
            key = _limited_text(key, f"{field_name} key", MAX_JSON_KEY_TEXT)
            result[key] = _json_value(item, f"{field_name}.{key}", depth + 1)
        return result
    raise ValueError(f"{field_name} must contain only JSON-compatible values")


def _json_object(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    normalized = _json_value(value, field_name)
    assert isinstance(normalized, dict)
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_JSON_OBJECT_BYTES:
        raise ValueError(f"{field_name} exceeds the JSON size limit")
    return normalized


def _positive_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _status(value: object, field_name: str, allowed: frozenset[str]) -> str:
    status = _text(value, field_name)
    if status not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"unknown {field_name} {status!r}; expected one of: {choices}")
    return status


def _strict_mapping(data: object, fields: tuple[str, ...], model_name: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{model_name} must be a JSON object")
    if any(type(name) is not str for name in data):
        raise ValueError(f"{model_name} field names must be strings")
    actual = set(data)
    expected = set(fields)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ValueError(f"{model_name} is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{model_name} has unknown fields: {', '.join(unknown)}")
    return data


def _wechat_identity(value: object, field_name: str = "wechat_identity") -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    if any(type(name) is not str for name in value):
        raise ValueError(f"{field_name} field names must be strings")
    unknown = sorted(set(value) - WECHAT_IDENTITY_FIELDS)
    if unknown:
        raise ValueError(f"{field_name} has unknown fields: {', '.join(unknown)}")

    identity: dict[str, str] = {}
    for name in ("account_id", "nickname"):
        if name not in value:
            continue
        item = value[name]
        if type(item) is not str:
            raise ValueError(f"{field_name}.{name} must be a string")
        if not item.strip():
            raise ValueError(f"{field_name}.{name} must not be empty")
        if len(item) > MAX_WECHAT_IDENTITY_TEXT:
            raise ValueError(
                f"{field_name}.{name} must not exceed {MAX_WECHAT_IDENTITY_TEXT} characters"
            )
        if any(unicodedata.category(character) == "Cc" for character in item):
            raise ValueError(f"{field_name}.{name} contains an invalid control character")
        identity[name] = item
    return identity


@dataclass(frozen=True)
class AgentHeartbeat:
    tenant_id: str
    agent_id: str
    channel_account_id: str
    status: str
    observed_at: str
    wechat_identity: dict[str, str] = field(default_factory=dict)
    capabilities: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("tenant_id", "agent_id", "channel_account_id", "observed_at"):
            _text(getattr(self, name), name)
        _status(self.status, "status", HEARTBEAT_STATUSES)
        object.__setattr__(
            self,
            "wechat_identity",
            _wechat_identity(self.wechat_identity),
        )
        object.__setattr__(
            self,
            "capabilities",
            _json_object(self.capabilities, "capabilities"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel_account_id": self.channel_account_id,
            "status": self.status,
            "observed_at": self.observed_at,
            "wechat_identity": dict(self.wechat_identity),
            "capabilities": dict(self.capabilities),
        }

    @classmethod
    def from_dict(cls, data: object) -> "AgentHeartbeat":
        fields = ("tenant_id", "agent_id", "channel_account_id", "status", "observed_at")
        if not isinstance(data, Mapping):
            raise ValueError(f"{cls.__name__} must be a JSON object")
        if any(type(name) is not str for name in data):
            raise ValueError(f"{cls.__name__} field names must be strings")
        actual = set(data)
        missing = sorted(set(fields) - actual)
        unknown = sorted(actual - set(fields) - {"wechat_identity", "capabilities"})
        if missing:
            raise ValueError(f"{cls.__name__} is missing fields: {', '.join(missing)}")
        if unknown:
            raise ValueError(f"{cls.__name__} has unknown fields: {', '.join(unknown)}")
        return cls(
            **{name: data[name] for name in fields},  # type: ignore[arg-type]
            wechat_identity=data.get("wechat_identity", {}),  # type: ignore[arg-type]
            capabilities=data.get("capabilities", {}),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class InboundEnvelope:
    tenant_id: str
    agent_id: str
    channel_account_id: str
    external_message_id: str
    conversation_external_id: str
    sender_external_id: str
    message_type: str
    content: str
    occurred_at: str

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "external_message_id",
            "conversation_external_id",
            "sender_external_id",
            "message_type",
            "occurred_at",
        ):
            _text(getattr(self, name), name)
        _content(self.content, "content")

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel_account_id": self.channel_account_id,
            "external_message_id": self.external_message_id,
            "conversation_external_id": self.conversation_external_id,
            "sender_external_id": self.sender_external_id,
            "message_type": self.message_type,
            "content": self.content,
            "occurred_at": self.occurred_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "InboundEnvelope":
        fields = (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "external_message_id",
            "conversation_external_id",
            "sender_external_id",
            "message_type",
            "content",
            "occurred_at",
        )
        values = _strict_mapping(data, fields, cls.__name__)
        return cls(**{name: values[name] for name in fields})  # type: ignore[arg-type]


@dataclass(frozen=True)
class ContactSnapshotItem:
    external_contact_id: str
    display_name: str
    remark: str
    kind: str
    status: str
    raw: dict[str, object]

    def __post_init__(self) -> None:
        _limited_text(self.external_contact_id, "external_contact_id", MAX_ID_TEXT)
        _limited_text(self.display_name, "display_name", MAX_NAME_TEXT, allow_empty=True)
        _limited_text(self.remark, "remark", MAX_REMARK_TEXT, allow_empty=True)
        _status(self.kind, "kind", CONTACT_KINDS)
        _status(self.status, "status", CONTACT_STATUSES)
        object.__setattr__(self, "raw", _json_object(self.raw, "raw"))

    def to_dict(self) -> dict[str, object]:
        return {
            "external_contact_id": self.external_contact_id,
            "display_name": self.display_name,
            "remark": self.remark,
            "kind": self.kind,
            "status": self.status,
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, data: object) -> "ContactSnapshotItem":
        fields = ("external_contact_id", "display_name", "remark", "kind", "status", "raw")
        values = _strict_mapping(data, fields, cls.__name__)
        return cls(**{name: values[name] for name in fields})  # type: ignore[arg-type]


@dataclass(frozen=True)
class ContactSnapshot:
    tenant_id: str
    agent_id: str
    channel_account_id: str
    snapshot_id: str
    observed_at: str
    contacts: list[ContactSnapshotItem]

    def __post_init__(self) -> None:
        for name in ("tenant_id", "agent_id", "channel_account_id", "snapshot_id"):
            _limited_text(getattr(self, name), name, MAX_ID_TEXT)
        _limited_text(self.observed_at, "observed_at", MAX_TIMESTAMP_TEXT)
        if type(self.contacts) is not list:
            raise ValueError("contacts must be an array")
        if len(self.contacts) > MAX_SNAPSHOT_ITEMS:
            raise ValueError(f"contacts must not contain more than {MAX_SNAPSHOT_ITEMS} items")
        if any(not isinstance(item, ContactSnapshotItem) for item in self.contacts):
            raise ValueError("contacts must contain only ContactSnapshotItem values")
        object.__setattr__(self, "contacts", list(self.contacts))

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel_account_id": self.channel_account_id,
            "snapshot_id": self.snapshot_id,
            "observed_at": self.observed_at,
            "contacts": [item.to_dict() for item in self.contacts],
        }

    @classmethod
    def from_dict(cls, data: object) -> "ContactSnapshot":
        fields = (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "snapshot_id",
            "observed_at",
            "contacts",
        )
        values = _strict_mapping(data, fields, cls.__name__)
        contacts = values["contacts"]
        if type(contacts) is not list:
            raise ValueError("ContactSnapshot field 'contacts' must be an array")
        return cls(
            tenant_id=values["tenant_id"],  # type: ignore[arg-type]
            agent_id=values["agent_id"],  # type: ignore[arg-type]
            channel_account_id=values["channel_account_id"],  # type: ignore[arg-type]
            snapshot_id=values["snapshot_id"],  # type: ignore[arg-type]
            observed_at=values["observed_at"],  # type: ignore[arg-type]
            contacts=[ContactSnapshotItem.from_dict(item) for item in contacts],
        )


@dataclass(frozen=True)
class FriendRequestSnapshotItem:
    external_request_id: str
    display_name: str
    verification_message: str
    requested_at: str
    status: str
    raw: dict[str, object]

    def __post_init__(self) -> None:
        _limited_text(self.external_request_id, "external_request_id", MAX_ID_TEXT)
        _limited_text(self.display_name, "display_name", MAX_NAME_TEXT, allow_empty=True)
        _limited_text(
            self.verification_message,
            "verification_message",
            MAX_VERIFICATION_TEXT,
            allow_empty=True,
        )
        _limited_text(self.requested_at, "requested_at", MAX_TIMESTAMP_TEXT)
        _status(self.status, "status", FRIEND_REQUEST_STATUSES)
        object.__setattr__(self, "raw", _json_object(self.raw, "raw"))

    def to_dict(self) -> dict[str, object]:
        return {
            "external_request_id": self.external_request_id,
            "display_name": self.display_name,
            "verification_message": self.verification_message,
            "requested_at": self.requested_at,
            "status": self.status,
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, data: object) -> "FriendRequestSnapshotItem":
        fields = (
            "external_request_id",
            "display_name",
            "verification_message",
            "requested_at",
            "status",
            "raw",
        )
        values = _strict_mapping(data, fields, cls.__name__)
        return cls(**{name: values[name] for name in fields})  # type: ignore[arg-type]


@dataclass(frozen=True)
class FriendRequestSnapshot:
    tenant_id: str
    agent_id: str
    channel_account_id: str
    snapshot_id: str
    observed_at: str
    requests: list[FriendRequestSnapshotItem]

    def __post_init__(self) -> None:
        for name in ("tenant_id", "agent_id", "channel_account_id", "snapshot_id"):
            _limited_text(getattr(self, name), name, MAX_ID_TEXT)
        _limited_text(self.observed_at, "observed_at", MAX_TIMESTAMP_TEXT)
        if type(self.requests) is not list:
            raise ValueError("requests must be an array")
        if len(self.requests) > MAX_SNAPSHOT_ITEMS:
            raise ValueError(f"requests must not contain more than {MAX_SNAPSHOT_ITEMS} items")
        if any(not isinstance(item, FriendRequestSnapshotItem) for item in self.requests):
            raise ValueError("requests must contain only FriendRequestSnapshotItem values")
        object.__setattr__(self, "requests", list(self.requests))

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel_account_id": self.channel_account_id,
            "snapshot_id": self.snapshot_id,
            "observed_at": self.observed_at,
            "requests": [item.to_dict() for item in self.requests],
        }

    @classmethod
    def from_dict(cls, data: object) -> "FriendRequestSnapshot":
        fields = (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "snapshot_id",
            "observed_at",
            "requests",
        )
        values = _strict_mapping(data, fields, cls.__name__)
        requests = values["requests"]
        if type(requests) is not list:
            raise ValueError("FriendRequestSnapshot field 'requests' must be an array")
        return cls(
            tenant_id=values["tenant_id"],  # type: ignore[arg-type]
            agent_id=values["agent_id"],  # type: ignore[arg-type]
            channel_account_id=values["channel_account_id"],  # type: ignore[arg-type]
            snapshot_id=values["snapshot_id"],  # type: ignore[arg-type]
            observed_at=values["observed_at"],  # type: ignore[arg-type]
            requests=[FriendRequestSnapshotItem.from_dict(item) for item in requests],
        )


@dataclass(frozen=True)
class OperationCommand:
    tenant_id: str
    agent_id: str
    channel_account_id: str
    operation_id: str
    lease_token: str
    operation_type: str
    attempt: int
    payload: dict[str, object]

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "operation_id",
            "lease_token",
        ):
            _limited_text(getattr(self, name), name, MAX_ID_TEXT)
        _status(self.operation_type, "operation_type", OPERATION_TYPES)
        _positive_int(self.attempt, "attempt")
        object.__setattr__(self, "payload", _json_object(self.payload, "payload"))

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel_account_id": self.channel_account_id,
            "operation_id": self.operation_id,
            "lease_token": self.lease_token,
            "operation_type": self.operation_type,
            "attempt": self.attempt,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: object) -> "OperationCommand":
        fields = (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "operation_id",
            "lease_token",
            "operation_type",
            "attempt",
            "payload",
        )
        values = _strict_mapping(data, fields, cls.__name__)
        return cls(**{name: values[name] for name in fields})  # type: ignore[arg-type]


@dataclass(frozen=True)
class OperationStart:
    tenant_id: str
    agent_id: str
    channel_account_id: str
    operation_id: str
    lease_token: str
    attempt: int

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "operation_id",
            "lease_token",
        ):
            _limited_text(getattr(self, name), name, MAX_ID_TEXT)
        _positive_int(self.attempt, "attempt")

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel_account_id": self.channel_account_id,
            "operation_id": self.operation_id,
            "lease_token": self.lease_token,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: object) -> "OperationStart":
        fields = (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "operation_id",
            "lease_token",
            "attempt",
        )
        values = _strict_mapping(data, fields, cls.__name__)
        return cls(**{name: values[name] for name in fields})  # type: ignore[arg-type]


@dataclass(frozen=True)
class OperationResult:
    tenant_id: str
    agent_id: str
    channel_account_id: str
    operation_id: str
    lease_token: str
    attempt: int
    status: str
    completed_at: str
    error: str | None
    evidence: dict[str, object]

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "operation_id",
            "lease_token",
        ):
            _limited_text(getattr(self, name), name, MAX_ID_TEXT)
        _positive_int(self.attempt, "attempt")
        _status(self.status, "status", OPERATION_RESULT_STATUSES)
        _limited_text(self.completed_at, "completed_at", MAX_TIMESTAMP_TEXT)
        if self.error is not None:
            _limited_text(self.error, "error", MAX_VERIFICATION_TEXT)
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("error must be null when status is succeeded")
        if self.status in {"failed", "uncertain"} and self.error is None:
            raise ValueError(f"error is required when status is {self.status}")
        object.__setattr__(self, "evidence", _json_object(self.evidence, "evidence"))

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel_account_id": self.channel_account_id,
            "operation_id": self.operation_id,
            "lease_token": self.lease_token,
            "attempt": self.attempt,
            "status": self.status,
            "completed_at": self.completed_at,
            "error": self.error,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: object) -> "OperationResult":
        fields = (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "operation_id",
            "lease_token",
            "attempt",
            "status",
            "completed_at",
            "error",
            "evidence",
        )
        values = _strict_mapping(data, fields, cls.__name__)
        return cls(**{name: values[name] for name in fields})  # type: ignore[arg-type]


@dataclass(frozen=True)
class DeliveryCommand:
    tenant_id: str
    agent_id: str
    channel_account_id: str
    delivery_id: str
    lease_token: str
    conversation_external_id: str
    content: str
    status: str
    attempt: int

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "delivery_id",
            "lease_token",
            "conversation_external_id",
        ):
            _text(getattr(self, name), name)
        _content(self.content, "content")
        _status(self.status, "status", DELIVERY_COMMAND_STATUSES)
        _positive_int(self.attempt, "attempt")

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel_account_id": self.channel_account_id,
            "delivery_id": self.delivery_id,
            "lease_token": self.lease_token,
            "conversation_external_id": self.conversation_external_id,
            "content": self.content,
            "status": self.status,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: object) -> "DeliveryCommand":
        fields = (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "delivery_id",
            "lease_token",
            "conversation_external_id",
            "content",
            "status",
            "attempt",
        )
        values = _strict_mapping(data, fields, cls.__name__)
        return cls(**{name: values[name] for name in fields})  # type: ignore[arg-type]


@dataclass(frozen=True)
class DeliveryStart:
    tenant_id: str
    agent_id: str
    channel_account_id: str
    delivery_id: str
    lease_token: str
    attempt: int

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "delivery_id",
            "lease_token",
        ):
            _text(getattr(self, name), name)
        _positive_int(self.attempt, "attempt")

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel_account_id": self.channel_account_id,
            "delivery_id": self.delivery_id,
            "lease_token": self.lease_token,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: object) -> "DeliveryStart":
        fields = (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "delivery_id",
            "lease_token",
            "attempt",
        )
        values = _strict_mapping(data, fields, cls.__name__)
        return cls(**{name: values[name] for name in fields})  # type: ignore[arg-type]


@dataclass(frozen=True)
class DeliveryResult:
    tenant_id: str
    agent_id: str
    channel_account_id: str
    delivery_id: str
    lease_token: str
    attempt: int
    status: str
    completed_at: str
    error: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "delivery_id",
            "lease_token",
            "completed_at",
        ):
            _text(getattr(self, name), name)
        _positive_int(self.attempt, "attempt")
        _status(self.status, "status", DELIVERY_RESULT_STATUSES)
        if self.error is not None:
            _content(self.error, "error")
        if self.status == "sent" and self.error is not None:
            raise ValueError("error must be null when status is sent")
        if self.status in {"failed", "uncertain"} and self.error is None:
            raise ValueError(f"error is required when status is {self.status}")

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "channel_account_id": self.channel_account_id,
            "delivery_id": self.delivery_id,
            "lease_token": self.lease_token,
            "attempt": self.attempt,
            "status": self.status,
            "completed_at": self.completed_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: object) -> "DeliveryResult":
        fields = (
            "tenant_id",
            "agent_id",
            "channel_account_id",
            "delivery_id",
            "lease_token",
            "attempt",
            "status",
            "completed_at",
            "error",
        )
        values = _strict_mapping(data, fields, cls.__name__)
        return cls(**{name: values[name] for name in fields})  # type: ignore[arg-type]


class ResponseLike(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


Opener = Callable[..., ResponseLike] | object


class CenterAgentClient:
    def __init__(
        self,
        base_url: str,
        agent_token: str,
        tenant_id: str,
        agent_id: str,
        channel_account_id: str,
        timeout: float = 15.0,
        opener: Opener | None = None,
        *,
        allow_insecure_http: bool = False,
        retry_policy: RetryPolicy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        base_url = _text(base_url, "base_url").rstrip("/")
        token = _text(agent_token, "agent_token")
        tenant_id = _text(tenant_id, "tenant_id")
        agent_id = _text(agent_id, "agent_id")
        channel_account_id = _text(channel_account_id, "channel_account_id")
        parsed = urlsplit(base_url)
        allowed_schemes = {"https"}
        if allow_insecure_http:
            allowed_schemes.add("http")
        if parsed.scheme.lower() not in allowed_schemes:
            raise ValueError("base_url must use https")
        if not parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must contain a host and must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number")

        self.base_url = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), "", ""))
        self.agent_token = token
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self.channel_account_id = channel_account_id
        self.timeout = float(timeout)
        self._opener = opener if opener is not None else build_opener()
        self._retry_policy = retry_policy or RetryPolicy(
            max_attempts=4,
            base_delay=1.0,
            max_delay=30.0,
            should_retry=_retryable_center_error,
        )
        self._circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=60.0,
        )

    def heartbeat(
        self,
        heartbeat: AgentHeartbeat,
        *,
        stable_id: str | None = None,
    ) -> dict[str, object]:
        if not isinstance(heartbeat, AgentHeartbeat):
            raise TypeError("heartbeat must be an AgentHeartbeat")
        self._require_bound_identity(heartbeat)
        if stable_id is None:
            stable_id = f"{heartbeat.agent_id}:{int(time.time() // 60)}"
        else:
            stable_id = _text(stable_id, "stable_id")
        response = self._post("/v1/agent/heartbeat", heartbeat.to_dict(), stable_id)
        return self._heartbeat_response(response)

    def report_inbound(self, envelope: InboundEnvelope) -> None:
        if not isinstance(envelope, InboundEnvelope):
            raise TypeError("envelope must be an InboundEnvelope")
        self._require_bound_identity(envelope)
        response = self._post(
            "/v1/agent/inbound",
            envelope.to_dict(),
            envelope.external_message_id,
        )
        self._require_accepted(response, "report_inbound")

    def sync_contacts(self, snapshot: ContactSnapshot) -> None:
        if not isinstance(snapshot, ContactSnapshot):
            raise TypeError("snapshot must be a ContactSnapshot")
        self._require_bound_identity(snapshot)
        response = self._post(
            "/v1/agent/contacts/sync",
            snapshot.to_dict(),
            snapshot.snapshot_id,
        )
        self._require_accepted(response, "sync_contacts")

    def sync_friend_requests(self, snapshot: FriendRequestSnapshot) -> None:
        if not isinstance(snapshot, FriendRequestSnapshot):
            raise TypeError("snapshot must be a FriendRequestSnapshot")
        self._require_bound_identity(snapshot)
        response = self._post(
            "/v1/agent/friend-requests/sync",
            snapshot.to_dict(),
            snapshot.snapshot_id,
        )
        self._require_accepted(response, "sync_friend_requests")

    def claim_deliveries(self, limit: int) -> list[DeliveryCommand]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        response = self._post(
            "/v1/agent/deliveries/claim",
            {
                "tenant_id": self.tenant_id,
                "agent_id": self.agent_id,
                "channel_account_id": self.channel_account_id,
                "limit": limit,
            },
            f"claim:{uuid.uuid4().hex}",
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("claim_deliveries response must be a JSON object")
        deliveries = response.get("deliveries")
        if type(deliveries) is not list:
            raise RuntimeError("claim_deliveries response field 'deliveries' must be an array")
        try:
            return [DeliveryCommand.from_dict(item) for item in deliveries]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(self._safe_text(f"invalid delivery command: {exc}")) from exc

    def complete_delivery(self, result: DeliveryResult) -> None:
        if not isinstance(result, DeliveryResult):
            raise TypeError("result must be a DeliveryResult")
        self._require_bound_identity(result)
        response = self._post(
            "/v1/agent/deliveries/complete",
            result.to_dict(),
            f"{result.delivery_id}:{result.attempt}",
        )
        self._require_accepted(response, "complete_delivery")

    def start_delivery(self, start: DeliveryStart) -> bool:
        if not isinstance(start, DeliveryStart):
            raise TypeError("start must be a DeliveryStart")
        self._require_bound_identity(start)
        response = self._post(
            "/v1/agent/deliveries/start",
            start.to_dict(),
            f"{start.delivery_id}:{start.attempt}:start",
        )
        self._require_accepted(response, "start_delivery")
        send_allowed = response.get("send_allowed", True)
        if type(send_allowed) is not bool:
            raise RuntimeError("start_delivery response field 'send_allowed' must be a boolean")
        return send_allowed

    def claim_operations(self, limit: int) -> list[OperationCommand]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        response = self._post(
            "/v1/agent/operations/claim",
            {
                "tenant_id": self.tenant_id,
                "agent_id": self.agent_id,
                "channel_account_id": self.channel_account_id,
                "limit": limit,
            },
            f"operations:claim:{uuid.uuid4().hex}",
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("claim_operations response must be a JSON object")
        operations = response.get("operations")
        if type(operations) is not list:
            raise RuntimeError("claim_operations response field 'operations' must be an array")
        if len(operations) > limit:
            raise RuntimeError("claim_operations response exceeds the requested limit")
        try:
            commands = [OperationCommand.from_dict(item) for item in operations]
            for command in commands:
                self._require_bound_identity(command)
            return commands
        except (TypeError, ValueError) as exc:
            raise RuntimeError(self._safe_text(f"invalid operation command: {exc}")) from exc

    def start_operation(self, start: OperationStart) -> None:
        if not isinstance(start, OperationStart):
            raise TypeError("start must be an OperationStart")
        self._require_bound_identity(start)
        response = self._post(
            "/v1/agent/operations/start",
            start.to_dict(),
            f"{start.operation_id}:{start.attempt}:start",
        )
        self._require_accepted(response, "start_operation")

    def complete_operation(self, result: OperationResult) -> None:
        if not isinstance(result, OperationResult):
            raise TypeError("result must be an OperationResult")
        self._require_bound_identity(result)
        response = self._post(
            "/v1/agent/operations/complete",
            result.to_dict(),
            f"{result.operation_id}:{result.attempt}",
        )
        self._require_accepted(response, "complete_operation")

    def _require_bound_identity(self, message: object) -> None:
        actual = (
            getattr(message, "tenant_id", None),
            getattr(message, "agent_id", None),
            getattr(message, "channel_account_id", None),
        )
        expected = (self.tenant_id, self.agent_id, self.channel_account_id)
        if actual != expected:
            raise ValueError("protocol identity does not match the configured Agent binding")

    def _post(self, path: str, payload: Mapping[str, object], idempotency_key: str) -> object:
        def call_with_retry() -> object:
            try:
                return self._retry_policy.call(
                    lambda: self._post_once(path, payload, idempotency_key)
                )
            except CenterHTTPError as exc:
                if not _retryable_center_error(exc):
                    return exc
                raise

        result = self._circuit_breaker.call(call_with_retry)
        if isinstance(result, CenterHTTPError):
            raise result
        return result

    def _post_once(
        self,
        path: str,
        payload: Mapping[str, object],
        idempotency_key: str,
    ) -> object:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.agent_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Idempotency-Key": self._header_value(idempotency_key),
            },
        )
        try:
            response = self._open(request)
            try:
                status = getattr(response, "status", None)
                if status is None and hasattr(response, "getcode"):
                    status = response.getcode()
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except HTTPError as exc:
            raw = exc.read(MAX_ERROR_TEXT + 1)
            raise CenterHTTPError(
                exc.code,
                self._safe_text(f"center service returned HTTP {exc.code}: {self._decode(raw)}")
            ) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise CenterTransportError(
                self._safe_text(f"center service request failed: {exc}")
            ) from exc
        except Exception as exc:
            raise CenterTransportError(
                self._safe_text(f"center service request failed: {exc}")
            ) from exc

        if type(status) is not int or not 200 <= status < 300:
            raise CenterHTTPError(
                int(status) if type(status) is int else 0,
                self._safe_text(f"center service returned HTTP {status}: {self._decode(raw)}")
            )
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("center service response exceeded size limit")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("center service returned invalid JSON") from exc

    def _open(self, request: Request) -> ResponseLike:
        open_method = getattr(self._opener, "open", None)
        if callable(open_method):
            return open_method(request, timeout=self.timeout)
        if callable(self._opener):
            return self._opener(request, timeout=self.timeout)
        raise TypeError("opener must be callable or provide open()")

    def _require_accepted(self, response: object, operation: str) -> None:
        if not isinstance(response, Mapping):
            raise RuntimeError(f"{operation} response must be a JSON object")
        accepted = response.get("accepted")
        if type(accepted) is not bool:
            raise RuntimeError(f"{operation} response field 'accepted' must be a boolean")
        if not accepted:
            raise RuntimeError(f"center service did not accept {operation}")

    def _heartbeat_response(self, response: object) -> dict[str, object]:
        fields = (
            "accepted",
            "identity_status",
            "operations_allowed",
            "wechat_identity",
        )
        try:
            values = _strict_mapping(response, fields, "heartbeat response")
            identity_status = _status(
                values["identity_status"],
                "identity_status",
                HEARTBEAT_IDENTITY_STATUSES,
            )
            identity = _wechat_identity(
                values["wechat_identity"],
                "heartbeat response.wechat_identity",
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        accepted = values["accepted"]
        if type(accepted) is not bool:
            raise RuntimeError("heartbeat response field 'accepted' must be a boolean")
        if not accepted:
            raise RuntimeError("center service did not accept heartbeat")
        operations_allowed = values["operations_allowed"]
        if type(operations_allowed) is not bool:
            raise RuntimeError(
                "heartbeat response field 'operations_allowed' must be a boolean"
            )
        if operations_allowed and identity_status != "verified":
            raise RuntimeError(
                "heartbeat response cannot allow operations for an unverified identity"
            )
        if identity_status == "verified" and not identity:
            raise RuntimeError(
                "heartbeat response cannot verify an empty WeChat identity"
            )
        return {
            "accepted": accepted,
            "identity_status": identity_status,
            "operations_allowed": operations_allowed,
            "wechat_identity": identity,
        }

    def _safe_text(self, value: object) -> str:
        text = str(value).replace(self.agent_token, "[REDACTED]")
        text = " ".join(text.split())
        if len(text) > MAX_ERROR_TEXT:
            return f"{text[:MAX_ERROR_TEXT]}..."
        return text

    @staticmethod
    def _decode(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _header_value(value: str) -> str:
        value = _text(value, "idempotency_key")
        try:
            value.encode("ascii")
        except UnicodeEncodeError:
            return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
        return value
