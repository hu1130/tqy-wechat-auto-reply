from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import threading
from typing import Any, Iterable, Iterator, Mapping
import uuid

from sqlalchemy import (
    Boolean,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    event,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator[datetime]):
    """Store timezone-aware UTC datetimes on both PostgreSQL and SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return _as_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return _as_utc(value)


JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")
MAX_MANAGEMENT_LIST_LIMIT = 500
CONTACT_KINDS = frozenset({"friend", "group"})
CONTACT_STATUSES = frozenset({"active", "inactive", "deleted"})
CHAT_POLICY_MODES = frozenset({"ai_auto", "manual", "paused", "blocked"})
FRIEND_REQUEST_STATUSES = frozenset({"pending", "approved", "rejected", "expired"})
OPERATION_TYPES = frozenset({"accept_friend", "reject_friend", "send_welcome"})
OPERATION_STATUSES = frozenset(
    {"queued", "claimed", "executing", "succeeded", "failed", "uncertain", "cancelled"}
)
WECHAT_IDENTITY_CONFIG_KEY = "_center_wechat_identity_v1"
WECHAT_IDENTITY_STATUSES = frozenset(
    {"unavailable", "pending", "verified", "mismatch"}
)
PERSONAL_WECHAT_PROVISIONING_CONFIG_KEY = "_center_personal_wechat_v1"


class ProvisioningConflictError(RuntimeError):
    pass


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class ChannelAccount(Base):
    __tablename__ = "channel_accounts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel",
            "external_account_id",
            name="uq_channel_account_external",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AgentInstance(Base):
    __tablename__ = "agent_instances"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_agent_id", name="uq_agent_tenant_external"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    channel_account_id: Mapped[str] = mapped_column(
        ForeignKey("channel_accounts.id"), nullable=False, index=True
    )
    external_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="offline")
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AgentActivation(Base):
    __tablename__ = "agent_activations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_agent_activation_tenant_idempotency",
        ),
        UniqueConstraint("code_hash", name="uq_agent_activation_code_hash"),
        CheckConstraint(
            "expires_in_seconds BETWEEN 300 AND 86400",
            name="ck_agent_activation_expiry_seconds",
        ),
        Index(
            "ix_agent_activation_consume",
            "code_hash",
            "consumed_at",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agent_instances.id"), nullable=False, index=True
    )
    channel_account_id: Mapped[str] = mapped_column(
        ForeignKey("channel_accounts.id"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_in_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    consumed_device_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    consumed_device_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    consumed_agent_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AccessCredential(Base):
    __tablename__ = "access_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key_id", name="uq_access_credential_tenant_key"),
        CheckConstraint("subject_type IN ('user', 'agent')", name="ck_access_subject_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_account_id",
            "external_conversation_id",
            name="uq_conversation_external",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    channel_account_id: Mapped[str] = mapped_column(
        ForeignKey("channel_accounts.id"), nullable=False, index=True
    )
    external_conversation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_external_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    customer_display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_account_id",
            "external_message_id",
            name="uq_message_external",
        ),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="ck_message_direction"),
        Index("ix_messages_tenant_conversation_received", "tenant_id", "conversation_id", "received_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    channel_account_id: Mapped[str] = mapped_column(
        ForeignKey("channel_accounts.id"), nullable=False, index=True
    )
    external_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="inbound")
    message_type: Mapped[str] = mapped_column(String(64), nullable=False, default="text")
    sender_external_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class ReplyJob(Base):
    __tablename__ = "reply_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_message_id", name="uq_reply_job_source_message"),
        CheckConstraint(
            "status IN ('queued', 'processing', 'generated', 'failed')",
            name="ck_reply_job_status",
        ),
        Index("ix_reply_job_claim", "tenant_id", "status", "available_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    source_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    generated_delivery_id: Mapped[str | None] = mapped_column(
        ForeignKey("delivery_jobs.id"),
        nullable=True,
    )
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class BotProfile(Base):
    __tablename__ = "bot_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_bot_profile_tenant_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_kb_tenant_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class DeliveryJob(Base):
    __tablename__ = "delivery_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_delivery_tenant_dedupe"),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'sending', 'completed', 'failed', 'uncertain')",
            name="ck_delivery_status",
        ),
        Index("ix_delivery_claim", "tenant_id", "channel_account_id", "status", "available_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    channel_account_id: Mapped[str] = mapped_column(
        ForeignKey("channel_accounts.id"), nullable=False, index=True
    )
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    claimed_by_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_instances.id"), nullable=True
    )
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolution_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class ChannelContact(Base):
    __tablename__ = "channel_contacts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_account_id",
            "external_contact_id",
            name="uq_channel_contact_external",
        ),
        CheckConstraint("kind IN ('friend', 'group')", name="ck_channel_contact_kind"),
        CheckConstraint(
            "status IN ('active', 'inactive', 'deleted')",
            name="ck_channel_contact_status",
        ),
        Index(
            "ix_channel_contact_list",
            "tenant_id",
            "channel_account_id",
            "kind",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    channel_account_id: Mapped[str] = mapped_column(
        ForeignKey("channel_accounts.id"), nullable=False, index=True
    )
    external_contact_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    remark: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    first_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class ChatPolicy(Base):
    __tablename__ = "chat_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "contact_id", name="uq_chat_policy_tenant_contact"),
        CheckConstraint(
            "mode IN ('ai_auto', 'manual', 'paused', 'blocked')",
            name="ck_chat_policy_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    contact_id: Mapped[str] = mapped_column(
        ForeignKey("channel_contacts.id"), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    welcome_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class FriendRequest(Base):
    __tablename__ = "friend_requests"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_account_id",
            "external_request_id",
            name="uq_friend_request_external",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="ck_friend_request_status",
        ),
        Index(
            "ix_friend_request_list",
            "tenant_id",
            "channel_account_id",
            "status",
            "requested_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    channel_account_id: Mapped[str] = mapped_column(
        ForeignKey("channel_accounts.id"), nullable=False, index=True
    )
    external_request_id: Mapped[str] = mapped_column(String(255), nullable=False)
    requester_external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    verification_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class OperationLimit(Base):
    __tablename__ = "operation_limits"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_account_id",
            name="uq_operation_limit_tenant_channel",
        ),
        CheckConstraint("batch_size BETWEEN 1 AND 100", name="ck_operation_limit_batch"),
        CheckConstraint(
            "min_interval_seconds BETWEEN 1 AND 3600",
            name="ck_operation_limit_interval",
        ),
        CheckConstraint(
            "max_per_hour BETWEEN 1 AND 10000",
            name="ck_operation_limit_hour",
        ),
        CheckConstraint(
            "max_per_day BETWEEN 1 AND 100000",
            name="ck_operation_limit_day",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    channel_account_id: Mapped[str] = mapped_column(
        ForeignKey("channel_accounts.id"), nullable=False, index=True
    )
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    min_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    max_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    welcome_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    auto_enable_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class OperationJob(Base):
    __tablename__ = "operation_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_operation_tenant_idempotency"),
        UniqueConstraint(
            "tenant_id",
            "friend_request_id",
            "operation_type",
            name="uq_operation_friend_request_type",
        ),
        CheckConstraint(
            "operation_type IN ('accept_friend', 'reject_friend', 'send_welcome')",
            name="ck_operation_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'executing', 'succeeded', 'failed', "
            "'uncertain', 'cancelled')",
            name="ck_operation_status",
        ),
        Index(
            "ix_operation_claim",
            "tenant_id",
            "channel_account_id",
            "status",
            "available_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    channel_account_id: Mapped[str] = mapped_column(
        ForeignKey("channel_accounts.id"), nullable=False, index=True
    )
    friend_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("friend_requests.id"), nullable=True, index=True
    )
    depends_on_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("operation_jobs.id"), nullable=True
    )
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    claimed_by_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_instances.id"), nullable=True
    )
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolution_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class OperationAttempt(Base):
    __tablename__ = "operation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "operation_job_id",
            "attempt_number",
            name="uq_operation_attempt_number",
        ),
        CheckConstraint(
            "status IN ('claimed', 'executing', 'succeeded', 'failed', 'uncertain')",
            name="ck_operation_attempt_status",
        ),
        Index(
            "ix_operation_attempt_rate",
            "tenant_id",
            "channel_account_id",
            "claimed_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    channel_account_id: Mapped[str] = mapped_column(
        ForeignKey("channel_accounts.id"), nullable=False, index=True
    )
    operation_job_id: Mapped[str] = mapped_column(
        ForeignKey("operation_jobs.id"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_instances.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="claimed")
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class ApiCredential(Base):
    __tablename__ = "api_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "name", name="uq_api_credential_tenant_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TenantSecret(Base):
    __tablename__ = "tenant_secrets"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_secret_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


@dataclass(frozen=True, slots=True)
class TenantMember:
    membership_id: str
    tenant_id: str
    user_id: str
    email: str
    display_name: str
    status: str
    role: str
    created_at: datetime
    updated_at: datetime


class TenantStore:
    """Tenant-scoped SQLAlchemy repository for the central service data layer."""

    def __init__(self, database_url: str) -> None:
        url = make_url(database_url)
        is_test_sqlite = url.drivername == "sqlite+pysqlite" and url.database == ":memory:"
        is_postgresql = url.drivername == "postgresql+psycopg"
        if not (is_test_sqlite or is_postgresql):
            raise ValueError(
                "DATABASE_URL must use postgresql+psycopg; "
                "sqlite+pysqlite:///:memory: is allowed only as the test backend"
            )

        engine_options: dict[str, Any] = {"pool_pre_ping": True}
        if is_test_sqlite:
            engine_options.update(
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        self.engine: Engine = create_engine(database_url, **engine_options)
        if is_test_sqlite:
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)

        self._is_postgresql = is_postgresql
        self._is_test_sqlite = is_test_sqlite
        self._sqlite_lock = threading.RLock()
        self._session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        lock = self._sqlite_lock if self._is_test_sqlite else nullcontext()
        with lock:
            with self._session_factory.begin() as session:
                yield session

    @contextmanager
    def _read_session(self) -> Iterator[Session]:
        lock = self._sqlite_lock if self._is_test_sqlite else nullcontext()
        with lock:
            with self._session_factory() as session:
                yield session

    @staticmethod
    def _require_text(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} must not be empty")
        return normalized

    @staticmethod
    def _validate_management_limit(limit: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        if not 1 <= limit <= MAX_MANAGEMENT_LIST_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {MAX_MANAGEMENT_LIST_LIMIT}"
            )
        return limit

    @staticmethod
    def _require_tenant(session: Session, tenant_id: str) -> Tenant:
        tenant = session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            raise LookupError("tenant not found")
        return tenant

    @staticmethod
    def _require_user(session: Session, user_id: str) -> User:
        user = session.scalar(select(User).where(User.id == user_id))
        if user is None:
            raise LookupError("user not found")
        return user

    @staticmethod
    def _require_channel_account(
        session: Session, tenant_id: str, channel_account_id: str
    ) -> ChannelAccount:
        account = session.scalar(
            select(ChannelAccount).where(
                ChannelAccount.id == channel_account_id,
                ChannelAccount.tenant_id == tenant_id,
            )
        )
        if account is None:
            raise LookupError("channel account not found for tenant")
        return account

    @staticmethod
    def _require_agent(session: Session, tenant_id: str, agent_id: str) -> AgentInstance:
        agent = session.scalar(
            select(AgentInstance).where(
                AgentInstance.id == agent_id,
                AgentInstance.tenant_id == tenant_id,
            )
        )
        if agent is None:
            raise LookupError("agent not found for tenant")
        return agent

    @staticmethod
    def _require_conversation(
        session: Session, tenant_id: str, conversation_id: str
    ) -> Conversation:
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == tenant_id,
            )
        )
        if conversation is None:
            raise LookupError("conversation not found for tenant")
        return conversation

    @staticmethod
    def _require_message(session: Session, tenant_id: str, message_id: str) -> Message:
        message = session.scalar(
            select(Message).where(Message.id == message_id, Message.tenant_id == tenant_id)
        )
        if message is None:
            raise LookupError("message not found for tenant")
        return message

    @staticmethod
    def _require_choice(value: str, field: str, choices: frozenset[str]) -> str:
        normalized = TenantStore._require_text(value, field)
        if normalized not in choices:
            raise ValueError(f"{field} must be one of: {', '.join(sorted(choices))}")
        return normalized

    @staticmethod
    def _require_bool(value: bool, field: str) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be a boolean")
        return value

    @staticmethod
    def _optional_datetime(value: Any, field: str, default: datetime) -> datetime:
        if value is None:
            return default
        if not isinstance(value, datetime):
            raise ValueError(f"{field} must be a datetime")
        return _as_utc(value)

    @staticmethod
    def _require_member(session: Session, tenant_id: str, user_id: str) -> Membership:
        membership = session.scalar(
            select(Membership).where(
                Membership.tenant_id == tenant_id,
                Membership.user_id == user_id,
            )
        )
        if membership is None:
            raise ValueError("actor is not a member of tenant")
        return membership

    @staticmethod
    def _require_contact(session: Session, tenant_id: str, contact_id: str) -> ChannelContact:
        contact = session.scalar(
            select(ChannelContact).where(
                ChannelContact.id == contact_id,
                ChannelContact.tenant_id == tenant_id,
            )
        )
        if contact is None:
            raise LookupError("channel contact not found for tenant")
        return contact

    @staticmethod
    def _require_friend_request(
        session: Session, tenant_id: str, request_id: str
    ) -> FriendRequest:
        request = session.scalar(
            select(FriendRequest).where(
                FriendRequest.id == request_id,
                FriendRequest.tenant_id == tenant_id,
            )
        )
        if request is None:
            raise LookupError("friend request not found for tenant")
        return request

    @staticmethod
    def _require_operation_job(
        session: Session,
        tenant_id: str,
        operation_job_id: str,
        *,
        lock: bool = False,
    ) -> OperationJob:
        statement = select(OperationJob).where(
                OperationJob.id == operation_job_id,
                OperationJob.tenant_id == tenant_id,
            )
        if lock:
            statement = statement.with_for_update()
        job = session.scalar(statement)
        if job is None:
            raise LookupError("operation job not found for tenant")
        return job

    @staticmethod
    def _append_audit_event(
        session: Session,
        tenant_id: str,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        actor_user_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> None:
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                details_json=dict(details or {}),
                created_at=_as_utc(created_at or utc_now()),
            )
        )

    def create_schema(self) -> None:
        from wechat_ai.center_migrations import upgrade_database

        upgrade_database(self.engine)

    def healthcheck(self) -> bool:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True

    def bootstrap_tenant_bundle(
        self,
        *,
        tenant_slug: str,
        tenant_name: str,
        owner_email: str,
        channel: str,
        external_account_id: str,
        agent_external_id: str,
        agent_name: str,
        tenant_key_id: str,
        tenant_key_prefix: str,
        tenant_key_hash: str,
        agent_key_id: str,
        agent_key_prefix: str,
        agent_key_hash: str,
    ) -> tuple[Tenant, User, ChannelAccount, AgentInstance]:
        with self._transaction() as session:
            normalized_email = self._require_text(owner_email, "owner_email").lower()
            tenant = Tenant(
                slug=self._require_text(tenant_slug, "tenant_slug").lower(),
                name=self._require_text(tenant_name, "tenant_name"),
                status="active",
            )
            session.add(tenant)
            session.flush()
            owner = session.scalar(select(User).where(User.email == normalized_email))
            if owner is None:
                owner = User(
                    email=normalized_email,
                    display_name="",
                    status="active",
                )
                session.add(owner)
                session.flush()
            elif owner.status != "active":
                raise ValueError("owner user is not active")
            membership = Membership(
                tenant_id=tenant.id,
                user_id=owner.id,
                role="owner",
            )
            channel_account = ChannelAccount(
                tenant_id=tenant.id,
                channel=self._require_text(channel, "channel"),
                external_account_id=self._require_text(
                    external_account_id,
                    "external_account_id",
                ),
                display_name="",
                status="active",
                config_json={},
            )
            session.add_all((membership, channel_account))
            session.flush()
            agent = AgentInstance(
                tenant_id=tenant.id,
                channel_account_id=channel_account.id,
                external_agent_id=self._require_text(
                    agent_external_id,
                    "agent_external_id",
                ),
                name=agent_name.strip(),
                status="offline",
                capabilities_json={},
            )
            session.add(agent)
            session.flush()
            session.add_all(
                (
                    AccessCredential(
                        tenant_id=tenant.id,
                        subject_type="user",
                        subject_id=owner.id,
                        key_id=self._require_text(tenant_key_id, "tenant_key_id"),
                        prefix=self._require_text(tenant_key_prefix, "tenant_key_prefix"),
                        key_hash=self._require_text(tenant_key_hash, "tenant_key_hash"),
                    ),
                    AccessCredential(
                        tenant_id=tenant.id,
                        subject_type="agent",
                        subject_id=agent.id,
                        key_id=self._require_text(agent_key_id, "agent_key_id"),
                        prefix=self._require_text(agent_key_prefix, "agent_key_prefix"),
                        key_hash=self._require_text(agent_key_hash, "agent_key_hash"),
                    ),
                    AuditEvent(
                        tenant_id=tenant.id,
                        actor_user_id=owner.id,
                        action="tenant.bootstrapped",
                        entity_type="tenant",
                        entity_id=tenant.id,
                        details_json={
                            "channel_account_id": channel_account.id,
                            "agent_id": agent.id,
                        },
                    ),
                )
            )
            session.flush()
        return tenant, owner, channel_account, agent

    def provision_personal_wechat_bundle(
        self,
        *,
        tenant_slug: str,
        tenant_name: str,
        owner_email: str,
        channel_external_account_id: str,
        agent_external_id: str,
        tenant_key_id: str,
        tenant_key_prefix: str,
        tenant_key_hash: str,
    ) -> tuple[Tenant, User, ChannelAccount, AgentInstance, bool]:
        normalized_slug = self._require_text(tenant_slug, "tenant_slug").lower()
        normalized_name = self._require_text(tenant_name, "tenant_name")
        normalized_email = self._require_text(owner_email, "owner_email").lower()
        normalized_channel_id = self._require_text(
            channel_external_account_id, "channel_external_account_id"
        )
        normalized_agent_id = self._require_text(
            agent_external_id, "agent_external_id"
        )
        normalized_key_id = self._require_text(tenant_key_id, "tenant_key_id")
        normalized_key_prefix = self._require_text(
            tenant_key_prefix, "tenant_key_prefix"
        )
        normalized_key_hash = self._require_text(tenant_key_hash, "tenant_key_hash")
        if len(normalized_slug) > 100 or len(normalized_name) > 200:
            raise ValueError("provisioning tenant fields exceed storage limits")
        if len(normalized_email) > 320:
            raise ValueError("owner_email exceeds storage limits")

        with self._transaction() as session:
            if self._is_postgresql:
                session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(:tenant_slug, 0))"
                    ),
                    {"tenant_slug": normalized_slug},
                )
            tenant = session.scalar(
                select(Tenant).where(Tenant.slug == normalized_slug)
            )
            if tenant is not None:
                if tenant.status != "active":
                    raise ProvisioningConflictError("provisioning_contract_conflict")
                if tenant.name != normalized_name:
                    tenant.name = normalized_name
                    tenant.updated_at = utc_now()
                owner = session.scalar(
                    select(User)
                    .join(Membership, Membership.user_id == User.id)
                    .where(
                        Membership.tenant_id == tenant.id,
                        Membership.role == "owner",
                        User.email == normalized_email,
                        User.status == "active",
                    )
                )
                channel_account = session.scalar(
                    select(ChannelAccount).where(
                        ChannelAccount.tenant_id == tenant.id,
                        ChannelAccount.channel == "wechat_desktop",
                        ChannelAccount.external_account_id == normalized_channel_id,
                    )
                )
                agent = session.scalar(
                    select(AgentInstance).where(
                        AgentInstance.tenant_id == tenant.id,
                        AgentInstance.external_agent_id == normalized_agent_id,
                    )
                )
                marker = (
                    channel_account.config_json.get(
                        PERSONAL_WECHAT_PROVISIONING_CONFIG_KEY
                    )
                    if channel_account is not None
                    and isinstance(channel_account.config_json, Mapping)
                    else None
                )
                credential = session.scalar(
                    select(AccessCredential).where(
                        AccessCredential.tenant_id == tenant.id,
                        AccessCredential.key_id == normalized_key_id,
                        AccessCredential.prefix == normalized_key_prefix,
                        AccessCredential.status == "active",
                    )
                )
                if (
                    owner is None
                    or channel_account is None
                    or marker != {"tenant_slug": normalized_slug}
                    or agent is None
                    or agent.channel_account_id != channel_account.id
                    or credential is None
                    or credential.subject_type != "user"
                    or credential.subject_id != owner.id
                    or credential.key_hash != normalized_key_hash
                ):
                    raise ProvisioningConflictError("provisioning_contract_conflict")
                return tenant, owner, channel_account, agent, False

            owner = session.scalar(select(User).where(User.email == normalized_email))
            if owner is None:
                owner = User(email=normalized_email, display_name="", status="active")
                session.add(owner)
                session.flush()
            elif owner.status != "active":
                raise ProvisioningConflictError("provisioning_contract_conflict")

            tenant = Tenant(slug=normalized_slug, name=normalized_name, status="active")
            session.add(tenant)
            session.flush()
            membership = Membership(tenant_id=tenant.id, user_id=owner.id, role="owner")
            channel_account = ChannelAccount(
                tenant_id=tenant.id,
                channel="wechat_desktop",
                external_account_id=normalized_channel_id,
                display_name="",
                status="active",
                config_json={
                    PERSONAL_WECHAT_PROVISIONING_CONFIG_KEY: {
                        "tenant_slug": normalized_slug
                    }
                },
            )
            session.add_all((membership, channel_account))
            session.flush()
            agent = AgentInstance(
                tenant_id=tenant.id,
                channel_account_id=channel_account.id,
                external_agent_id=normalized_agent_id,
                name="Personal WeChat Agent",
                status="offline",
                capabilities_json={},
            )
            session.add(agent)
            session.flush()
            session.add(
                AccessCredential(
                    tenant_id=tenant.id,
                    subject_type="user",
                    subject_id=owner.id,
                    key_id=normalized_key_id,
                    prefix=normalized_key_prefix,
                    key_hash=normalized_key_hash,
                )
            )
            self._append_audit_event(
                session,
                tenant.id,
                actor_user_id=owner.id,
                action="tenant.personal_wechat_provisioned",
                entity_type="tenant",
                entity_id=tenant.id,
                details={
                    "channel_account_id": channel_account.id,
                    "agent_id": agent.id,
                },
            )
            session.flush()
            return tenant, owner, channel_account, agent, True

    def create_tenant(self, slug: str, name: str, *, status: str = "active") -> Tenant:
        tenant = Tenant(
            slug=self._require_text(slug, "slug").lower(),
            name=self._require_text(name, "name"),
            status=self._require_text(status, "status"),
        )
        with self._transaction() as session:
            session.add(tenant)
            session.flush()
        return tenant

    def create_user(
        self, email: str, *, display_name: str = "", status: str = "active"
    ) -> User:
        user = User(
            email=self._require_text(email, "email").lower(),
            display_name=display_name.strip(),
            status=self._require_text(status, "status"),
        )
        with self._transaction() as session:
            session.add(user)
            session.flush()
        return user

    def add_membership(self, tenant_id: str, user_id: str, role: str) -> Membership:
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_user(session, user_id)
            membership = Membership(
                tenant_id=tenant_id,
                user_id=user_id,
                role=self._require_text(role, "role"),
            )
            session.add(membership)
            session.flush()
        return membership

    def get_membership(self, tenant_id: str, user_id: str) -> Membership | None:
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return session.scalar(
                select(Membership).where(
                    Membership.tenant_id == tenant_id,
                    Membership.user_id == user_id,
                )
            )

    def get_tenant(self, tenant_id: str) -> Tenant:
        with self._read_session() as session:
            return self._require_tenant(session, tenant_id)

    def list_tenant_members(
        self, tenant_id: str, *, limit: int = 100
    ) -> list[TenantMember]:
        checked_limit = self._validate_management_limit(limit)
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            rows = session.execute(
                select(Membership, User)
                .join(User, User.id == Membership.user_id)
                .where(Membership.tenant_id == tenant_id)
                .order_by(User.email, Membership.id)
                .limit(checked_limit)
            ).all()
            return [
                TenantMember(
                    membership_id=membership.id,
                    tenant_id=membership.tenant_id,
                    user_id=user.id,
                    email=user.email,
                    display_name=user.display_name,
                    status=user.status,
                    role=membership.role,
                    created_at=membership.created_at,
                    updated_at=membership.updated_at,
                )
                for membership, user in rows
            ]

    def list_channel_accounts(
        self, tenant_id: str, *, limit: int = 100
    ) -> list[ChannelAccount]:
        checked_limit = self._validate_management_limit(limit)
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return list(
                session.scalars(
                    select(ChannelAccount)
                    .where(ChannelAccount.tenant_id == tenant_id)
                    .order_by(
                        ChannelAccount.channel,
                        ChannelAccount.external_account_id,
                        ChannelAccount.id,
                    )
                    .limit(checked_limit)
                )
            )

    def list_agent_instances(
        self, tenant_id: str, *, limit: int = 100
    ) -> list[AgentInstance]:
        checked_limit = self._validate_management_limit(limit)
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return list(
                session.scalars(
                    select(AgentInstance)
                    .where(AgentInstance.tenant_id == tenant_id)
                    .order_by(
                        AgentInstance.external_agent_id,
                        AgentInstance.id,
                    )
                    .limit(checked_limit)
                )
            )

    def create_channel_account(
        self,
        tenant_id: str,
        channel: str,
        external_account_id: str,
        *,
        display_name: str = "",
        status: str = "active",
        config: Mapping[str, Any] | None = None,
    ) -> ChannelAccount:
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            account = ChannelAccount(
                tenant_id=tenant_id,
                channel=self._require_text(channel, "channel"),
                external_account_id=self._require_text(
                    external_account_id, "external_account_id"
                ),
                display_name=display_name.strip(),
                status=self._require_text(status, "status"),
                config_json=dict(config or {}),
            )
            session.add(account)
            session.flush()
        return account

    def register_agent(
        self,
        tenant_id: str,
        channel_account_id: str,
        external_agent_id: str,
        *,
        name: str = "",
        status: str = "offline",
        capabilities: Mapping[str, Any] | None = None,
    ) -> AgentInstance:
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_channel_account(session, tenant_id, channel_account_id)
            agent = AgentInstance(
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                external_agent_id=self._require_text(external_agent_id, "external_agent_id"),
                name=name.strip(),
                status=self._require_text(status, "status"),
                capabilities_json=dict(capabilities or {}),
            )
            session.add(agent)
            session.flush()
        return agent

    def heartbeat_agent(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        status: str = "online",
        capabilities: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> AgentInstance:
        heartbeat_at = _as_utc(now or utc_now())
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            agent = self._require_agent(session, tenant_id, agent_id)
            agent.status = self._require_text(status, "status")
            if capabilities is not None:
                agent.capabilities_json = dict(capabilities)
            agent.last_heartbeat_at = heartbeat_at
            agent.updated_at = heartbeat_at
            session.flush()
        return agent

    @staticmethod
    def _normalize_wechat_identity(identity: Mapping[str, Any]) -> dict[str, str]:
        if not isinstance(identity, Mapping):
            raise TypeError("wechat_identity must be an object")
        unknown = set(identity) - {"account_id", "nickname"}
        if unknown:
            raise ValueError("wechat_identity contains unsupported fields")
        result: dict[str, str] = {}
        for field, maximum in (("account_id", 255), ("nickname", 200)):
            value = identity.get(field, "")
            if not isinstance(value, str):
                raise TypeError(f"wechat_identity.{field} must be a string")
            normalized = value.strip()
            if "\r" in normalized or "\n" in normalized:
                raise ValueError(f"wechat_identity.{field} must be single-line text")
            if len(normalized) > maximum:
                raise ValueError(
                    f"wechat_identity.{field} cannot exceed {maximum} characters"
                )
            if normalized:
                result[field] = normalized
        return result

    @staticmethod
    def _wechat_identity_state(account: ChannelAccount) -> dict[str, Any]:
        config = account.config_json if isinstance(account.config_json, Mapping) else {}
        state = config.get(WECHAT_IDENTITY_CONFIG_KEY, {})
        return dict(state) if isinstance(state, Mapping) else {}

    @classmethod
    def channel_wechat_identity_metadata(
        cls, account: ChannelAccount | Mapping[str, Any]
    ) -> dict[str, Any]:
        if isinstance(account, Mapping):
            config = account.get("config_json", {})
            state_value = (
                config.get(WECHAT_IDENTITY_CONFIG_KEY, {})
                if isinstance(config, Mapping)
                else {}
            )
            state = dict(state_value) if isinstance(state_value, Mapping) else {}
        else:
            state = cls._wechat_identity_state(account)
        status = str(state.get("status", "unavailable"))
        if status not in WECHAT_IDENTITY_STATUSES:
            status = "unavailable"
        observed_value = state.get("observed", {})
        try:
            observed = cls._normalize_wechat_identity(
                observed_value if isinstance(observed_value, Mapping) else {}
            )
        except (TypeError, ValueError):
            observed = {}
            status = "unavailable"
        return {
            "status": status,
            "observed_account_id": observed.get("account_id", ""),
            "observed_nickname": observed.get("nickname", ""),
            "account_id": observed.get("account_id", ""),
            "nickname": observed.get("nickname", ""),
            "observed_at": state.get("observed_at"),
            "verified_at": state.get("verified_at"),
        }

    def observe_channel_wechat_identity(
        self,
        tenant_id: str,
        channel_account_id: str,
        identity: Mapping[str, Any],
        identity_fingerprint: str,
        *,
        now: datetime | None = None,
    ) -> tuple[ChannelAccount, dict[str, Any]]:
        normalized_identity = self._normalize_wechat_identity(identity)
        if not isinstance(identity_fingerprint, str):
            raise TypeError("identity_fingerprint must be a string")
        normalized_fingerprint = identity_fingerprint.strip()
        if normalized_identity:
            if not normalized_fingerprint:
                raise ValueError("identity_fingerprint is required")
            if len(normalized_fingerprint) > 128:
                raise ValueError("identity_fingerprint cannot exceed 128 characters")
        elif normalized_fingerprint:
            raise ValueError("identity_fingerprint requires a wechat identity")
        observed_at = _as_utc(now or utc_now())

        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            statement = select(ChannelAccount).where(
                ChannelAccount.id == channel_account_id,
                ChannelAccount.tenant_id == tenant_id,
            )
            if self._is_postgresql:
                statement = statement.with_for_update()
            account = session.scalar(statement)
            if account is None:
                raise LookupError("channel account not found for tenant")

            state = self._wechat_identity_state(account)
            if not normalized_identity:
                state["status"] = "unavailable"
            else:
                verified_fingerprint = str(state.get("verified_fingerprint", ""))
                if not verified_fingerprint:
                    state["status"] = "pending"
                elif hmac.compare_digest(
                    verified_fingerprint, normalized_fingerprint
                ):
                    state["status"] = "verified"
                else:
                    state["status"] = "mismatch"
                state["observed"] = normalized_identity
                state["observed_fingerprint"] = normalized_fingerprint
                state["observed_at"] = observed_at.isoformat()

            config = dict(account.config_json or {})
            config[WECHAT_IDENTITY_CONFIG_KEY] = state
            account.config_json = config
            account.updated_at = observed_at
            session.flush()
            metadata = self.channel_wechat_identity_metadata(account)
        return account, metadata

    def get_channel_wechat_identity(
        self, tenant_id: str, channel_account_id: str
    ) -> dict[str, Any]:
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            account = self._require_channel_account(
                session, tenant_id, channel_account_id
            )
            return self.channel_wechat_identity_metadata(account)

    def confirm_channel_wechat_identity(
        self,
        tenant_id: str,
        channel_account_id: str,
        actor_user_id: str,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> tuple[ChannelAccount, dict[str, Any], bool]:
        normalized_key = self._require_text(idempotency_key, "idempotency_key")
        if len(normalized_key) > 200:
            raise ValueError("idempotency_key cannot exceed 200 characters")
        verified_at = _as_utc(now or utc_now())

        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_member(session, tenant_id, actor_user_id)
            statement = select(ChannelAccount).where(
                ChannelAccount.id == channel_account_id,
                ChannelAccount.tenant_id == tenant_id,
            )
            if self._is_postgresql:
                statement = statement.with_for_update()
            account = session.scalar(statement)
            if account is None:
                raise LookupError("channel account not found for tenant")

            state = self._wechat_identity_state(account)
            observed = self._normalize_wechat_identity(
                state.get("observed", {})
                if isinstance(state.get("observed", {}), Mapping)
                else {}
            )
            observed_fingerprint = str(state.get("observed_fingerprint", ""))
            previous_confirmation = state.get("last_confirmation", {})
            if isinstance(previous_confirmation, Mapping) and str(
                previous_confirmation.get("idempotency_key", "")
            ) == normalized_key:
                if hmac.compare_digest(
                    str(previous_confirmation.get("fingerprint", "")),
                    observed_fingerprint,
                ):
                    return account, self.channel_wechat_identity_metadata(account), False
                raise ValueError(
                    "idempotency_key was already used for another wechat identity"
                )

            previous_status = str(state.get("status", "unavailable"))
            if previous_status not in {"pending", "mismatch"}:
                raise ValueError("wechat identity is not pending confirmation")
            if not observed or not observed_fingerprint:
                raise ValueError("wechat identity observation is unavailable")

            state.update(
                {
                    "status": "verified",
                    "verified": observed,
                    "verified_fingerprint": observed_fingerprint,
                    "verified_at": verified_at.isoformat(),
                    "verified_by_user_id": actor_user_id,
                    "last_confirmation": {
                        "idempotency_key": normalized_key,
                        "fingerprint": observed_fingerprint,
                    },
                }
            )
            config = dict(account.config_json or {})
            config[WECHAT_IDENTITY_CONFIG_KEY] = state
            account.config_json = config
            account.updated_at = verified_at
            self._append_audit_event(
                session,
                tenant_id,
                actor_user_id=actor_user_id,
                action="channel_account.wechat_identity_confirmed",
                entity_type="channel_account",
                entity_id=account.id,
                details={
                    "previous_status": previous_status,
                    "account_id_available": bool(observed.get("account_id")),
                },
                created_at=verified_at,
            )
            session.flush()
            metadata = self.channel_wechat_identity_metadata(account)
        return account, metadata, True

    def get_agent(self, tenant_id: str, agent_id: str) -> AgentInstance:
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return self._require_agent(session, tenant_id, agent_id)

    def create_agent_activation(
        self,
        tenant_id: str,
        agent_id: str,
        channel_account_id: str,
        expires_in_seconds: int,
        code_hash: str,
        idempotency_key: str,
        actor_user_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[AgentActivation, bool]:
        if not isinstance(expires_in_seconds, int) or isinstance(expires_in_seconds, bool):
            raise ValueError("expires_in_seconds must be an integer")
        if not 300 <= expires_in_seconds <= 86400:
            raise ValueError("expires_in_seconds must be between 300 and 86400")
        normalized_hash = self._require_text(code_hash, "code_hash")
        if len(normalized_hash) > 128:
            raise ValueError("code_hash cannot exceed 128 characters")
        normalized_key = self._require_text(idempotency_key, "idempotency_key")
        if len(normalized_key) > 200:
            raise ValueError("idempotency_key cannot exceed 200 characters")
        created_at = _as_utc(now or utc_now())

        with self._transaction() as session:
            tenant_statement = select(Tenant).where(Tenant.id == tenant_id)
            if self._is_postgresql:
                tenant_statement = tenant_statement.with_for_update()
            if session.scalar(tenant_statement) is None:
                raise LookupError("tenant not found")
            self._require_member(session, tenant_id, actor_user_id)
            account = self._require_channel_account(session, tenant_id, channel_account_id)
            agent = self._require_agent(session, tenant_id, agent_id)
            if agent.channel_account_id != account.id:
                raise ValueError("agent is not bound to channel account")

            existing = session.scalar(
                select(AgentActivation).where(
                    AgentActivation.tenant_id == tenant_id,
                    AgentActivation.idempotency_key == normalized_key,
                )
            )
            if existing is not None:
                if (
                    existing.agent_id != agent.id
                    or existing.channel_account_id != account.id
                    or existing.expires_in_seconds != expires_in_seconds
                    or existing.code_hash != normalized_hash
                ):
                    raise ValueError("idempotency_key was already used for another activation")
                return existing, False

            activation = AgentActivation(
                tenant_id=tenant_id,
                agent_id=agent.id,
                channel_account_id=account.id,
                code_hash=normalized_hash,
                idempotency_key=normalized_key,
                expires_in_seconds=expires_in_seconds,
                expires_at=created_at + timedelta(seconds=expires_in_seconds),
                created_by_user_id=actor_user_id,
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(activation)
            session.flush()
            self._append_audit_event(
                session,
                tenant_id,
                actor_user_id=actor_user_id,
                action="agent_activation.created",
                entity_type="agent_activation",
                entity_id=activation.id,
                details={
                    "agent_id": agent.id,
                    "channel_account_id": account.id,
                    "expires_in_seconds": expires_in_seconds,
                },
                created_at=created_at,
            )
            return activation, True

    def consume_agent_activation(
        self,
        code_hash: str,
        device_hash: str,
        device_name: str,
        agent_version: str,
        agent_key_id: str,
        agent_key_prefix: str,
        agent_key_hash: str,
        *,
        now: datetime | None = None,
    ) -> tuple[AgentActivation, AgentInstance]:
        normalized_code_hash = self._require_text(code_hash, "code_hash")
        normalized_device_hash = self._require_text(device_hash, "device_hash")
        normalized_device_name = self._require_text(device_name, "device_name")
        normalized_agent_version = self._require_text(agent_version, "agent_version")
        normalized_key_id = self._require_text(agent_key_id, "agent_key_id")
        normalized_key_prefix = self._require_text(agent_key_prefix, "agent_key_prefix")
        normalized_key_hash = self._require_text(agent_key_hash, "agent_key_hash")
        if len(normalized_code_hash) > 128 or len(normalized_device_hash) > 128:
            raise ValueError("activation_failed")
        if len(normalized_device_name) > 200 or len(normalized_agent_version) > 100:
            raise ValueError("activation_failed")
        consumed_at = _as_utc(now or utc_now())

        with self._transaction() as session:
            statement = select(AgentActivation).where(
                AgentActivation.code_hash == normalized_code_hash
            )
            if self._is_postgresql:
                statement = statement.with_for_update()
            activation = session.scalar(statement)
            if (
                activation is None
                or activation.consumed_at is not None
                or activation.expires_at <= consumed_at
            ):
                raise ValueError("activation_failed")

            agent = session.scalar(
                select(AgentInstance).where(
                    AgentInstance.id == activation.agent_id,
                    AgentInstance.tenant_id == activation.tenant_id,
                )
            )
            if agent is None or agent.channel_account_id != activation.channel_account_id:
                raise ValueError("activation_failed")
            account = session.scalar(
                select(ChannelAccount.id).where(
                    ChannelAccount.id == activation.channel_account_id,
                    ChannelAccount.tenant_id == activation.tenant_id,
                )
            )
            if account is None:
                raise ValueError("activation_failed")

            session.execute(
                update(AccessCredential)
                .where(
                    AccessCredential.tenant_id == activation.tenant_id,
                    AccessCredential.subject_type == "agent",
                    AccessCredential.subject_id == agent.id,
                    AccessCredential.status == "active",
                )
                .values(status="revoked", updated_at=consumed_at)
            )
            session.add(
                AccessCredential(
                    tenant_id=activation.tenant_id,
                    subject_type="agent",
                    subject_id=agent.id,
                    key_id=normalized_key_id,
                    prefix=normalized_key_prefix,
                    key_hash=normalized_key_hash,
                    status="active",
                    created_at=consumed_at,
                    updated_at=consumed_at,
                )
            )
            activation.consumed_device_hash = normalized_device_hash
            activation.consumed_device_name = normalized_device_name
            activation.consumed_agent_version = normalized_agent_version
            activation.consumed_at = consumed_at
            activation.updated_at = consumed_at
            self._append_audit_event(
                session,
                activation.tenant_id,
                action="agent_activation.consumed",
                entity_type="agent_activation",
                entity_id=activation.id,
                details={
                    "agent_id": agent.id,
                    "channel_account_id": activation.channel_account_id,
                    "agent_version": normalized_agent_version,
                },
                created_at=consumed_at,
            )
            session.flush()
            return activation, agent

    def get_conversation(self, tenant_id: str, conversation_id: str) -> Conversation:
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return self._require_conversation(session, tenant_id, conversation_id)

    def store_access_credential(
        self,
        tenant_id: str,
        subject_type: str,
        subject_id: str,
        key_id: str,
        prefix: str,
        key_hash: str,
    ) -> AccessCredential:
        normalized_type = self._require_text(subject_type, "subject_type")
        if normalized_type not in {"user", "agent"}:
            raise ValueError("subject_type must be user or agent")
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            if normalized_type == "user":
                membership = session.scalar(
                    select(Membership).where(
                        Membership.tenant_id == tenant_id,
                        Membership.user_id == subject_id,
                    )
                )
                if membership is None:
                    raise ValueError("credential user is not a member of tenant")
            else:
                self._require_agent(session, tenant_id, subject_id)
            credential = AccessCredential(
                tenant_id=tenant_id,
                subject_type=normalized_type,
                subject_id=self._require_text(subject_id, "subject_id"),
                key_id=self._require_text(key_id, "key_id"),
                prefix=self._require_text(prefix, "prefix"),
                key_hash=self._require_text(key_hash, "key_hash"),
            )
            session.add(credential)
            session.flush()
        return credential

    def get_access_credential(
        self,
        tenant_id: str,
        key_id: str,
        prefix: str,
    ) -> AccessCredential | None:
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return session.scalar(
                select(AccessCredential).where(
                    AccessCredential.tenant_id == tenant_id,
                    AccessCredential.key_id == key_id,
                    AccessCredential.prefix == prefix,
                    AccessCredential.status == "active",
                )
            )

    def mark_access_credential_used(
        self,
        tenant_id: str,
        credential_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        used_at = _as_utc(now or utc_now())
        with self._transaction() as session:
            result = session.execute(
                update(AccessCredential)
                .where(
                    AccessCredential.tenant_id == tenant_id,
                    AccessCredential.id == credential_id,
                    AccessCredential.status == "active",
                )
                .values(last_used_at=used_at, updated_at=used_at)
            )
            if result.rowcount != 1:
                raise LookupError("active access credential not found for tenant")

    @staticmethod
    def _resolve_inbound_contact(
        session: Session,
        tenant_id: str,
        channel_account_id: str,
        *,
        external_conversation_id: str,
        sender_external_id: str,
        customer_display_name: str,
    ) -> ChannelContact | None:
        group = session.scalar(
            select(ChannelContact).where(
                ChannelContact.tenant_id == tenant_id,
                ChannelContact.channel_account_id == channel_account_id,
                ChannelContact.kind == "group",
                ChannelContact.external_contact_id == external_conversation_id,
            )
        )
        if group is not None:
            return group

        external_candidates = list(
            dict.fromkeys(
                value
                for value in (sender_external_id.strip(), external_conversation_id)
                if value
            )
        )
        for external_id in external_candidates:
            friend = session.scalar(
                select(ChannelContact).where(
                    ChannelContact.tenant_id == tenant_id,
                    ChannelContact.channel_account_id == channel_account_id,
                    ChannelContact.kind == "friend",
                    ChannelContact.external_contact_id == external_id,
                )
            )
            if friend is not None:
                return friend

        name_candidates = list(
            dict.fromkeys(
                value
                for value in (customer_display_name.strip(), external_conversation_id)
                if value
            )
        )
        if not name_candidates:
            return None
        matches = list(
            session.scalars(
                select(ChannelContact)
                .where(
                    ChannelContact.tenant_id == tenant_id,
                    ChannelContact.channel_account_id == channel_account_id,
                    ChannelContact.kind == "friend",
                    or_(
                        ChannelContact.display_name.in_(name_candidates),
                        ChannelContact.remark.in_(name_candidates),
                    ),
                )
                .order_by(ChannelContact.id)
                .limit(2)
            )
        )
        return matches[0] if len(matches) == 1 else None

    def record_inbound_message(
        self,
        tenant_id: str,
        channel_account_id: str,
        external_conversation_id: str,
        external_message_id: str,
        *,
        content: str,
        raw_json: Mapping[str, Any],
        message_type: str = "text",
        sender_external_id: str = "",
        customer_display_name: str = "",
        received_at: datetime | None = None,
    ) -> tuple[Message, bool]:
        conversation_external_id = self._require_text(
            external_conversation_id, "external_conversation_id"
        )
        message_external_id = self._require_text(external_message_id, "external_message_id")

        for attempt in range(2):
            try:
                with self._transaction() as session:
                    self._require_tenant(session, tenant_id)
                    self._require_channel_account(session, tenant_id, channel_account_id)
                    existing = session.scalar(
                        select(Message).where(
                            Message.tenant_id == tenant_id,
                            Message.channel_account_id == channel_account_id,
                            Message.external_message_id == message_external_id,
                        )
                    )
                    if existing is not None:
                        return existing, False

                    conversation = session.scalar(
                        select(Conversation).where(
                            Conversation.tenant_id == tenant_id,
                            Conversation.channel_account_id == channel_account_id,
                            Conversation.external_conversation_id == conversation_external_id,
                        )
                    )
                    if conversation is None:
                        conversation = Conversation(
                            tenant_id=tenant_id,
                            channel_account_id=channel_account_id,
                            external_conversation_id=conversation_external_id,
                            customer_external_id=sender_external_id.strip(),
                            customer_display_name=customer_display_name.strip(),
                        )
                        session.add(conversation)
                        session.flush()

                    message = Message(
                        tenant_id=tenant_id,
                        conversation_id=conversation.id,
                        channel_account_id=channel_account_id,
                        external_message_id=message_external_id,
                        direction="inbound",
                        message_type=self._require_text(message_type, "message_type"),
                        sender_external_id=sender_external_id.strip(),
                        content=content,
                        raw_json=dict(raw_json),
                        received_at=_as_utc(received_at or utc_now()),
                    )
                    session.add(message)
                    conversation.updated_at = utc_now()
                    session.flush()
                    contact = self._resolve_inbound_contact(
                        session,
                        tenant_id,
                        channel_account_id,
                        external_conversation_id=conversation_external_id,
                        sender_external_id=sender_external_id,
                        customer_display_name=customer_display_name,
                    )
                    if contact is not None and contact.status == "active":
                        policy = session.scalar(
                            select(ChatPolicy).where(
                                ChatPolicy.tenant_id == tenant_id,
                                ChatPolicy.contact_id == contact.id,
                                ChatPolicy.mode == "ai_auto",
                            )
                        )
                        if policy is not None:
                            session.add(
                                ReplyJob(
                                    tenant_id=tenant_id,
                                    source_message_id=message.id,
                                )
                            )
                    session.flush()
                    return message, True
            except IntegrityError:
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")

    def list_active_tenant_ids(self) -> list[str]:
        with self._read_session() as session:
            return list(
                session.scalars(
                    select(Tenant.id).where(Tenant.status == "active").order_by(Tenant.id)
                )
            )

    def claim_reply_jobs(
        self,
        tenant_id: str,
        *,
        limit: int = 10,
        lease_seconds: int = 180,
        now: datetime | None = None,
    ) -> list[ReplyJob]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        claimed_at = _as_utc(now or utc_now())
        lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        claimable = or_(
            ReplyJob.status == "queued",
            and_(
                ReplyJob.status == "processing",
                ReplyJob.lease_expires_at.is_not(None),
                ReplyJob.lease_expires_at <= claimed_at,
            ),
        )
        statement = (
            select(ReplyJob.id)
            .where(
                ReplyJob.tenant_id == tenant_id,
                ReplyJob.available_at <= claimed_at,
                claimable,
            )
            .order_by(ReplyJob.created_at, ReplyJob.id)
            .limit(limit)
        )
        if self._is_postgresql:
            statement = statement.with_for_update(skip_locked=True)

        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            candidate_ids = list(session.scalars(statement))
            claimed_ids: list[str] = []
            for job_id in candidate_ids:
                lease_token = new_uuid()
                result = session.execute(
                    update(ReplyJob)
                    .where(
                        ReplyJob.id == job_id,
                        ReplyJob.tenant_id == tenant_id,
                        ReplyJob.available_at <= claimed_at,
                        or_(
                            ReplyJob.status == "queued",
                            and_(
                                ReplyJob.status == "processing",
                                ReplyJob.lease_expires_at.is_not(None),
                                ReplyJob.lease_expires_at <= claimed_at,
                            ),
                        ),
                    )
                    .values(
                        status="processing",
                        attempts=ReplyJob.attempts + 1,
                        lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                        error="",
                        updated_at=claimed_at,
                    )
                )
                if result.rowcount == 1:
                    claimed_ids.append(job_id)
            if not claimed_ids:
                return []
            return list(
                session.scalars(
                    select(ReplyJob)
                    .where(
                        ReplyJob.tenant_id == tenant_id,
                        ReplyJob.id.in_(claimed_ids),
                    )
                    .order_by(ReplyJob.created_at, ReplyJob.id)
                )
            )

    def list_reply_jobs(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ReplyJob]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            statement = select(ReplyJob).where(ReplyJob.tenant_id == tenant_id)
            if status is not None:
                statement = statement.where(ReplyJob.status == status)
            return list(
                session.scalars(
                    statement.order_by(ReplyJob.created_at, ReplyJob.id).limit(limit)
                )
            )

    def get_reply_job_context(
        self,
        tenant_id: str,
        reply_job_id: str,
        lease_token: str,
        *,
        max_messages: int = 20,
    ) -> tuple[ReplyJob, Message, Conversation, list[Message]]:
        if max_messages <= 0:
            raise ValueError("max_messages must be positive")
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            job = session.scalar(
                select(ReplyJob).where(
                    ReplyJob.tenant_id == tenant_id,
                    ReplyJob.id == reply_job_id,
                )
            )
            if job is None:
                raise LookupError("reply job not found for tenant")
            if job.status != "processing" or job.lease_token != lease_token:
                raise ValueError("reply job processing lease does not match")
            message = self._require_message(session, tenant_id, job.source_message_id)
            conversation = self._require_conversation(
                session,
                tenant_id,
                message.conversation_id,
            )
            history = list(
                session.scalars(
                    select(Message)
                    .where(
                        Message.tenant_id == tenant_id,
                        Message.conversation_id == conversation.id,
                        Message.received_at <= message.received_at,
                    )
                    .order_by(Message.received_at.desc(), Message.id.desc())
                    .limit(max_messages)
                )
            )
            history.reverse()
            return job, message, conversation, history

    def complete_reply_job(
        self,
        tenant_id: str,
        reply_job_id: str,
        lease_token: str,
        reply: str,
        *,
        now: datetime | None = None,
    ) -> DeliveryJob:
        generated_at = _as_utc(now or utc_now())
        normalized_reply = self._require_text(reply, "reply")
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            job = session.scalar(
                select(ReplyJob).where(
                    ReplyJob.tenant_id == tenant_id,
                    ReplyJob.id == reply_job_id,
                )
            )
            if job is None:
                raise LookupError("reply job not found for tenant")
            if job.status == "generated" and job.generated_delivery_id:
                delivery = session.scalar(
                    select(DeliveryJob).where(
                        DeliveryJob.tenant_id == tenant_id,
                        DeliveryJob.id == job.generated_delivery_id,
                    )
                )
                if delivery is None:
                    raise RuntimeError("generated reply job lost its delivery")
                return delivery
            if job.status != "processing" or job.lease_token != lease_token:
                raise ValueError("reply job processing lease does not match")
            if job.lease_expires_at is None or job.lease_expires_at <= generated_at:
                raise ValueError("reply job processing lease has expired")
            message = self._require_message(session, tenant_id, job.source_message_id)
            conversation = self._require_conversation(
                session,
                tenant_id,
                message.conversation_id,
            )
            if not self._message_has_ai_auto_policy(
                session,
                tenant_id,
                message,
                conversation,
            ):
                raise ValueError("reply policy no longer allows automatic sending")
            dedupe_key = f"ai-reply:{message.id}"
            delivery = session.scalar(
                select(DeliveryJob).where(
                    DeliveryJob.tenant_id == tenant_id,
                    DeliveryJob.dedupe_key == dedupe_key,
                )
            )
            if delivery is None:
                delivery = DeliveryJob(
                    tenant_id=tenant_id,
                    channel_account_id=message.channel_account_id,
                    conversation_id=conversation.id,
                    source_message_id=message.id,
                    dedupe_key=dedupe_key,
                    payload_json={
                        "conversation_external_id": conversation.external_conversation_id,
                        "content": normalized_reply,
                    },
                    available_at=generated_at,
                )
                session.add(delivery)
                session.flush()
            job.status = "generated"
            job.generated_delivery_id = delivery.id
            job.lease_token = None
            job.lease_expires_at = None
            job.error = ""
            job.updated_at = generated_at
            session.flush()
            return delivery

    def fail_reply_job(
        self,
        tenant_id: str,
        reply_job_id: str,
        lease_token: str,
        error: str,
        *,
        max_attempts: int = 4,
        retry_delay_seconds: float = 30.0,
        now: datetime | None = None,
    ) -> ReplyJob:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds cannot be negative")
        failed_at = _as_utc(now or utc_now())
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            job = session.scalar(
                select(ReplyJob).where(
                    ReplyJob.tenant_id == tenant_id,
                    ReplyJob.id == reply_job_id,
                )
            )
            if job is None:
                raise LookupError("reply job not found for tenant")
            if job.status != "processing" or job.lease_token != lease_token:
                raise ValueError("reply job processing lease does not match")
            job.error = str(error).strip()[:2000]
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = failed_at
            if job.attempts >= max_attempts:
                job.status = "failed"
            else:
                job.status = "queued"
                job.available_at = failed_at + timedelta(seconds=retry_delay_seconds)
            session.flush()
            return job

    def enqueue_delivery(
        self,
        tenant_id: str,
        channel_account_id: str,
        dedupe_key: str,
        payload: Mapping[str, Any],
        *,
        conversation_id: str | None = None,
        source_message_id: str | None = None,
        available_at: datetime | None = None,
    ) -> DeliveryJob:
        normalized_dedupe_key = self._require_text(dedupe_key, "dedupe_key")
        for attempt in range(2):
            try:
                with self._transaction() as session:
                    self._require_tenant(session, tenant_id)
                    self._require_channel_account(session, tenant_id, channel_account_id)
                    existing = session.scalar(
                        select(DeliveryJob).where(
                            DeliveryJob.tenant_id == tenant_id,
                            DeliveryJob.dedupe_key == normalized_dedupe_key,
                        )
                    )
                    if existing is not None:
                        return existing

                    if conversation_id is not None:
                        conversation = self._require_conversation(
                            session, tenant_id, conversation_id
                        )
                        if conversation.channel_account_id != channel_account_id:
                            raise ValueError("conversation belongs to another channel account")
                    if source_message_id is not None:
                        source_message = self._require_message(
                            session, tenant_id, source_message_id
                        )
                        if source_message.channel_account_id != channel_account_id:
                            raise ValueError("source message belongs to another channel account")

                    job = DeliveryJob(
                        tenant_id=tenant_id,
                        channel_account_id=channel_account_id,
                        conversation_id=conversation_id,
                        source_message_id=source_message_id,
                        dedupe_key=normalized_dedupe_key,
                        payload_json=dict(payload),
                        available_at=_as_utc(available_at or utc_now()),
                    )
                    session.add(job)
                    session.flush()
                    return job
            except IntegrityError:
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")

    @staticmethod
    def _claim_candidates_statement(
        tenant_id: str,
        channel_account_id: str,
        now: datetime,
        limit: int,
        *,
        use_skip_locked: bool,
    ) -> Any:
        claimable = or_(
            DeliveryJob.status == "queued",
            and_(
                DeliveryJob.status == "claimed",
                DeliveryJob.lease_expires_at.is_not(None),
                DeliveryJob.lease_expires_at <= now,
            ),
        )
        statement = (
            select(DeliveryJob.id)
            .where(
                DeliveryJob.tenant_id == tenant_id,
                DeliveryJob.channel_account_id == channel_account_id,
                DeliveryJob.available_at <= now,
                claimable,
            )
            .order_by(DeliveryJob.created_at, DeliveryJob.id)
            .limit(limit)
        )
        if use_skip_locked:
            statement = statement.with_for_update(skip_locked=True)
        return statement

    def claim_deliveries(
        self,
        tenant_id: str,
        agent_id: str,
        channel_account_id: str,
        *,
        limit: int = 10,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> list[DeliveryJob]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        claimed_at = _as_utc(now or utc_now())
        lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)

        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_channel_account(session, tenant_id, channel_account_id)
            agent = self._require_agent(session, tenant_id, agent_id)
            if agent.channel_account_id != channel_account_id:
                raise ValueError("agent is not bound to the requested channel account")

            candidate_ids = list(
                session.scalars(
                    self._claim_candidates_statement(
                        tenant_id,
                        channel_account_id,
                        claimed_at,
                        limit,
                        use_skip_locked=self._is_postgresql,
                    )
                )
            )
            claimed_ids: list[str] = []
            for job_id in candidate_ids:
                lease_token = new_uuid()
                result = session.execute(
                    update(DeliveryJob)
                    .where(
                        DeliveryJob.id == job_id,
                        DeliveryJob.tenant_id == tenant_id,
                        DeliveryJob.channel_account_id == channel_account_id,
                        DeliveryJob.available_at <= claimed_at,
                        or_(
                            DeliveryJob.status == "queued",
                            and_(
                                DeliveryJob.status == "claimed",
                                DeliveryJob.lease_expires_at.is_not(None),
                                DeliveryJob.lease_expires_at <= claimed_at,
                            ),
                        ),
                    )
                    .values(
                        status="claimed",
                        claimed_by_agent_id=agent_id,
                        lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                        attempts=DeliveryJob.attempts + 1,
                        updated_at=claimed_at,
                        error="",
                    )
                )
                if result.rowcount == 1:
                    claimed_ids.append(job_id)

            if not claimed_ids:
                return []
            return list(
                session.scalars(
                    select(DeliveryJob)
                    .where(
                        DeliveryJob.tenant_id == tenant_id,
                        DeliveryJob.id.in_(claimed_ids),
                    )
                    .order_by(DeliveryJob.created_at, DeliveryJob.id)
                )
            )

    def complete_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        agent_id: str,
        lease_token: str,
        attempt: int | None = None,
        *,
        result_status: str = "sent",
        error: str = "",
        now: datetime | None = None,
    ) -> DeliveryJob:
        completed_at = _as_utc(now or utc_now())
        if result_status not in {"sent", "failed", "uncertain"}:
            raise ValueError("result_status must be sent, failed, or uncertain")
        if result_status == "sent" and error:
            raise ValueError("sent delivery cannot include an error")
        if result_status != "sent" and not error.strip():
            raise ValueError(f"{result_status} delivery requires an error")
        stored_status = "completed" if result_status == "sent" else result_status
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            agent = self._require_agent(session, tenant_id, agent_id)
            job = session.scalar(
                select(DeliveryJob).where(
                    DeliveryJob.id == delivery_id,
                    DeliveryJob.tenant_id == tenant_id,
                )
            )
            if job is None:
                raise LookupError("delivery job not found for tenant")
            if agent.channel_account_id != job.channel_account_id:
                raise ValueError("agent is not bound to the delivery channel account")
            if job.claimed_by_agent_id != agent_id:
                raise ValueError("delivery job is claimed by another agent")
            if job.lease_token != lease_token:
                raise ValueError("delivery lease token does not match")
            if attempt is not None and job.attempts != attempt:
                raise ValueError("delivery attempt does not match")
            if job.status in {"completed", "failed", "uncertain"}:
                if job.status != stored_status:
                    raise ValueError("delivery already has a different terminal result")
                return job
            if job.status != "sending":
                raise ValueError("delivery job has not started sending")

            result = session.execute(
                update(DeliveryJob)
                .where(
                    DeliveryJob.id == delivery_id,
                    DeliveryJob.tenant_id == tenant_id,
                    DeliveryJob.status == "sending",
                    DeliveryJob.claimed_by_agent_id == agent_id,
                    DeliveryJob.lease_token == lease_token,
                )
                .values(
                    status=stored_status,
                    completed_at=completed_at,
                    error="" if result_status == "sent" else error.strip(),
                    updated_at=completed_at,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("delivery completion lost its sending ownership")
            if result_status == "sent" and job.conversation_id is not None:
                external_message_id = f"delivery:{job.id}"
                existing_message = session.scalar(
                    select(Message).where(
                        Message.tenant_id == tenant_id,
                        Message.channel_account_id == job.channel_account_id,
                        Message.external_message_id == external_message_id,
                    )
                )
                if existing_message is None:
                    session.add(
                        Message(
                            tenant_id=tenant_id,
                            conversation_id=job.conversation_id,
                            channel_account_id=job.channel_account_id,
                            external_message_id=external_message_id,
                            direction="outbound",
                            message_type="text",
                            sender_external_id="bot",
                            content=str(job.payload_json.get("content") or ""),
                            raw_json={"delivery_id": job.id},
                            received_at=completed_at,
                        )
                    )
                    session.flush()
            return job

    def start_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        agent_id: str,
        lease_token: str,
        attempt: int,
        *,
        now: datetime | None = None,
    ) -> DeliveryJob:
        started_at = _as_utc(now or utc_now())
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            agent = self._require_agent(session, tenant_id, agent_id)
            job = session.scalar(
                select(DeliveryJob).where(
                    DeliveryJob.id == delivery_id,
                    DeliveryJob.tenant_id == tenant_id,
                )
            )
            if job is None:
                raise LookupError("delivery job not found for tenant")
            if agent.channel_account_id != job.channel_account_id:
                raise ValueError("agent is not bound to the delivery channel account")
            if job.claimed_by_agent_id != agent_id:
                raise ValueError("delivery job is claimed by another agent")
            if job.lease_token != lease_token:
                raise ValueError("delivery lease token does not match")
            if job.attempts != attempt:
                raise ValueError("delivery attempt does not match")
            if job.status == "sending":
                return job
            if job.status == "failed" and job.error == "reply_policy_not_ai_auto":
                return job
            if job.status != "claimed":
                raise ValueError("delivery job is not claimed")
            if job.lease_expires_at is None or job.lease_expires_at <= started_at:
                raise ValueError("delivery lease has expired")

            if job.dedupe_key.startswith("ai-reply:"):
                message = self._require_message(session, tenant_id, job.source_message_id or "")
                conversation = self._require_conversation(
                    session,
                    tenant_id,
                    message.conversation_id,
                )
                if not self._message_has_ai_auto_policy(
                    session,
                    tenant_id,
                    message,
                    conversation,
                ):
                    job.status = "failed"
                    job.error = "reply_policy_not_ai_auto"
                    job.updated_at = started_at
                    session.flush()
                    return job

            result = session.execute(
                update(DeliveryJob)
                .where(
                    DeliveryJob.id == delivery_id,
                    DeliveryJob.tenant_id == tenant_id,
                    DeliveryJob.status == "claimed",
                    DeliveryJob.claimed_by_agent_id == agent_id,
                    DeliveryJob.lease_token == lease_token,
                    DeliveryJob.attempts == attempt,
                    DeliveryJob.lease_expires_at > started_at,
                )
                .values(
                    status="sending",
                    lease_expires_at=None,
                    updated_at=started_at,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("delivery start lost its lease")
            return job

    def _message_has_ai_auto_policy(
        self,
        session: Session,
        tenant_id: str,
        message: Message,
        conversation: Conversation,
    ) -> bool:
        contact = self._resolve_inbound_contact(
            session,
            tenant_id,
            message.channel_account_id,
            external_conversation_id=conversation.external_conversation_id,
            sender_external_id=message.sender_external_id,
            customer_display_name=conversation.customer_display_name,
        )
        if contact is None or contact.status != "active":
            return False
        return (
            session.scalar(
                select(ChatPolicy.id).where(
                    ChatPolicy.tenant_id == tenant_id,
                    ChatPolicy.contact_id == contact.id,
                    ChatPolicy.mode == "ai_auto",
                )
            )
            is not None
        )

    def resolve_uncertain_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        resolution: str,
        idempotency_key: str,
        *,
        actor_user_id: str,
        note: str,
        now: datetime | None = None,
    ) -> DeliveryJob:
        if resolution not in {"sent", "failed", "retry"}:
            raise ValueError("resolution must be sent, failed, or retry")
        resolved_at = _as_utc(now or utc_now())
        normalized_key = self._require_text(idempotency_key, "idempotency_key")
        normalized_note = self._require_text(note, "note")
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            membership = session.scalar(
                select(Membership).where(
                    Membership.tenant_id == tenant_id,
                    Membership.user_id == actor_user_id,
                )
            )
            if membership is None:
                raise ValueError("resolution actor is not a member of tenant")
            job = session.scalar(
                select(DeliveryJob).where(
                    DeliveryJob.tenant_id == tenant_id,
                    DeliveryJob.id == delivery_id,
                )
            )
            if job is None:
                raise LookupError("delivery job not found for tenant")
            if job.resolution_key == normalized_key:
                return job
            if job.status != "uncertain":
                raise ValueError("only uncertain delivery jobs can be resolved")

            if resolution == "retry":
                job.status = "queued"
                job.available_at = resolved_at
                job.claimed_by_agent_id = None
                job.lease_token = None
                job.lease_expires_at = None
                job.completed_at = None
                job.error = ""
            elif resolution == "sent":
                job.status = "completed"
                job.completed_at = resolved_at
                job.error = ""
            else:
                job.status = "failed"
                job.completed_at = resolved_at
                job.error = normalized_note
            job.resolution_key = normalized_key
            job.updated_at = resolved_at
            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    action="delivery.uncertain_resolved",
                    entity_type="delivery_job",
                    entity_id=delivery_id,
                    details_json={"resolution": resolution, "note": normalized_note},
                )
            )
            session.flush()
            return job

    def sync_channel_contacts(
        self,
        tenant_id: str,
        channel_account_id: str,
        contacts: Iterable[Mapping[str, Any]],
        observed_at: datetime,
    ) -> list[ChannelContact]:
        observed = self._optional_datetime(observed_at, "observed_at", utc_now())
        items = list(contacts)
        if len(items) > 5000:
            raise ValueError("contacts cannot contain more than 5000 items")
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_channel_account(session, tenant_id, channel_account_id)
            changed: dict[str, ChannelContact] = {}
            created_count = 0
            updated_count = 0
            for item in items:
                if not isinstance(item, Mapping):
                    raise ValueError("each contact must be a mapping")
                external_id = self._require_text(
                    str(item.get("external_contact_id", "")),
                    "external_contact_id",
                )
                kind = self._require_choice(str(item.get("kind", "")), "kind", CONTACT_KINDS)
                supplied_status = item.get("status")
                status = (
                    self._require_choice(str(supplied_status), "status", CONTACT_STATUSES)
                    if supplied_status is not None
                    else None
                )
                metadata = item.get("metadata", item.get("metadata_json"))
                if metadata is not None and not isinstance(metadata, Mapping):
                    raise ValueError("contact metadata must be a mapping")
                contact = session.scalar(
                    select(ChannelContact).where(
                        ChannelContact.tenant_id == tenant_id,
                        ChannelContact.channel_account_id == channel_account_id,
                        ChannelContact.external_contact_id == external_id,
                    )
                )
                if contact is None:
                    contact = ChannelContact(
                        tenant_id=tenant_id,
                        channel_account_id=channel_account_id,
                        external_contact_id=external_id,
                        kind=kind,
                        display_name=str(item.get("display_name", "")).strip(),
                        remark=str(item.get("remark", "")).strip(),
                        status=status or "active",
                        metadata_json=dict(metadata or {}),
                        first_observed_at=observed,
                        last_observed_at=observed,
                        created_at=observed,
                        updated_at=observed,
                    )
                    session.add(contact)
                    session.flush()
                    created_count += 1
                else:
                    contact.kind = kind
                    if "display_name" in item:
                        contact.display_name = str(item.get("display_name", "")).strip()
                    if "remark" in item:
                        contact.remark = str(item.get("remark", "")).strip()
                    if status is not None:
                        contact.status = status
                    if metadata is not None:
                        contact.metadata_json = dict(metadata)
                    if observed > contact.last_observed_at:
                        contact.last_observed_at = observed
                    contact.updated_at = observed
                    updated_count += 1
                changed[contact.id] = contact
            if changed:
                self._append_audit_event(
                    session,
                    tenant_id,
                    action="contacts.synced",
                    entity_type="channel_account",
                    entity_id=channel_account_id,
                    details={"created": created_count, "updated": updated_count},
                    created_at=observed,
                )
                session.flush()
            return sorted(
                changed.values(),
                key=lambda item: (item.display_name.casefold(), item.external_contact_id, item.id),
            )

    def list_channel_contacts_items(
        self,
        tenant_id: str,
        channel_account_id: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        query: str = "",
        limit: int = 100,
    ) -> list[ChannelContact]:
        checked_limit = self._validate_management_limit(limit)
        if kind is not None:
            kind = self._require_choice(kind, "kind", CONTACT_KINDS)
        if status is not None:
            status = self._require_choice(status, "status", CONTACT_STATUSES)
        normalized_query = query.strip()
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            statement = select(ChannelContact).where(ChannelContact.tenant_id == tenant_id)
            if channel_account_id is not None:
                self._require_channel_account(session, tenant_id, channel_account_id)
                statement = statement.where(
                    ChannelContact.channel_account_id == channel_account_id
                )
            if kind is not None:
                statement = statement.where(ChannelContact.kind == kind)
            if status is not None:
                statement = statement.where(ChannelContact.status == status)
            if normalized_query:
                statement = statement.where(
                    or_(
                        ChannelContact.external_contact_id.icontains(
                            normalized_query, autoescape=True
                        ),
                        ChannelContact.display_name.icontains(
                            normalized_query, autoescape=True
                        ),
                        ChannelContact.remark.icontains(normalized_query, autoescape=True),
                    )
                )
            return list(
                session.scalars(
                    statement.order_by(
                        func.lower(ChannelContact.display_name),
                        ChannelContact.external_contact_id,
                        ChannelContact.id,
                    ).limit(checked_limit)
                )
            )

    def set_chat_policy(
        self,
        tenant_id: str,
        contact_id: str,
        mode: str,
        welcome_enabled: bool,
        actor_user_id: str,
    ) -> ChatPolicy:
        normalized_mode = self._require_choice(mode, "mode", CHAT_POLICY_MODES)
        checked_welcome = self._require_bool(welcome_enabled, "welcome_enabled")
        changed_at = utc_now()
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_member(session, tenant_id, actor_user_id)
            self._require_contact(session, tenant_id, contact_id)
            policy = session.scalar(
                select(ChatPolicy).where(
                    ChatPolicy.tenant_id == tenant_id,
                    ChatPolicy.contact_id == contact_id,
                )
            )
            if policy is None:
                policy = ChatPolicy(
                    tenant_id=tenant_id,
                    contact_id=contact_id,
                    mode=normalized_mode,
                    welcome_enabled=checked_welcome,
                    created_at=changed_at,
                    updated_at=changed_at,
                )
                session.add(policy)
            else:
                policy.mode = normalized_mode
                policy.welcome_enabled = checked_welcome
                policy.updated_at = changed_at
            self._append_audit_event(
                session,
                tenant_id,
                actor_user_id=actor_user_id,
                action="chat_policy.updated",
                entity_type="channel_contact",
                entity_id=contact_id,
                details={"mode": normalized_mode, "welcome_enabled": checked_welcome},
                created_at=changed_at,
            )
            session.flush()
            return policy

    def batch_set_chat_policies(
        self,
        tenant_id: str,
        contact_ids: Iterable[str],
        mode: str,
        welcome_enabled: bool | None,
        actor_user_id: str,
    ) -> list[ChatPolicy]:
        normalized_mode = self._require_choice(mode, "mode", CHAT_POLICY_MODES)
        checked_welcome = (
            None
            if welcome_enabled is None
            else self._require_bool(welcome_enabled, "welcome_enabled")
        )
        normalized_ids = list(dict.fromkeys(str(item).strip() for item in contact_ids))
        if not normalized_ids or any(not item for item in normalized_ids):
            raise ValueError("contact_ids must not be empty")
        if len(normalized_ids) > MAX_MANAGEMENT_LIST_LIMIT:
            raise ValueError(f"contact_ids cannot contain more than {MAX_MANAGEMENT_LIST_LIMIT} items")
        changed_at = utc_now()
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_member(session, tenant_id, actor_user_id)
            contacts = list(
                session.scalars(
                    select(ChannelContact).where(
                        ChannelContact.tenant_id == tenant_id,
                        ChannelContact.id.in_(normalized_ids),
                    )
                )
            )
            if len(contacts) != len(normalized_ids):
                raise LookupError("one or more channel contacts were not found for tenant")
            existing = {
                item.contact_id: item
                for item in session.scalars(
                    select(ChatPolicy).where(
                        ChatPolicy.tenant_id == tenant_id,
                        ChatPolicy.contact_id.in_(normalized_ids),
                    )
                )
            }
            policies: list[ChatPolicy] = []
            for contact_id in normalized_ids:
                policy = existing.get(contact_id)
                if policy is None:
                    policy = ChatPolicy(
                        tenant_id=tenant_id,
                        contact_id=contact_id,
                        mode=normalized_mode,
                        welcome_enabled=checked_welcome if checked_welcome is not None else False,
                        created_at=changed_at,
                        updated_at=changed_at,
                    )
                    session.add(policy)
                else:
                    policy.mode = normalized_mode
                    if checked_welcome is not None:
                        policy.welcome_enabled = checked_welcome
                    policy.updated_at = changed_at
                policies.append(policy)
            self._append_audit_event(
                session,
                tenant_id,
                actor_user_id=actor_user_id,
                action="chat_policy.batch_updated",
                entity_type="channel_contact",
                entity_id="",
                details={
                    "contact_ids": normalized_ids,
                    "mode": normalized_mode,
                    "welcome_enabled": checked_welcome,
                },
                created_at=changed_at,
            )
            session.flush()
            return policies

    def list_chat_policies(
        self,
        tenant_id: str,
        contact_ids: Iterable[str] | None = None,
    ) -> list[ChatPolicy]:
        if isinstance(contact_ids, (str, bytes)):
            raise ValueError("contact_ids must be an iterable of IDs")
        normalized_ids = (
            list(dict.fromkeys(str(item).strip() for item in contact_ids))
            if contact_ids is not None
            else None
        )
        if normalized_ids is not None:
            if any(not item for item in normalized_ids):
                raise ValueError("contact_ids cannot contain empty IDs")
            if len(normalized_ids) > MAX_MANAGEMENT_LIST_LIMIT:
                raise ValueError(
                    f"contact_ids cannot contain more than {MAX_MANAGEMENT_LIST_LIMIT} items"
                )
            if not normalized_ids:
                return []
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            statement = select(ChatPolicy).where(ChatPolicy.tenant_id == tenant_id)
            if normalized_ids is not None:
                statement = statement.where(ChatPolicy.contact_id.in_(normalized_ids))
            return list(
                session.scalars(
                    statement.order_by(ChatPolicy.contact_id, ChatPolicy.id).limit(
                        MAX_MANAGEMENT_LIST_LIMIT
                    )
                )
            )

    def sync_friend_requests(
        self,
        tenant_id: str,
        channel_account_id: str,
        requests: Iterable[Mapping[str, Any]],
        observed_at: datetime,
    ) -> list[FriendRequest]:
        observed = self._optional_datetime(observed_at, "observed_at", utc_now())
        items = list(requests)
        if len(items) > 5000:
            raise ValueError("requests cannot contain more than 5000 items")
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_channel_account(session, tenant_id, channel_account_id)
            changed: dict[str, FriendRequest] = {}
            created_count = 0
            updated_count = 0
            for item in items:
                if not isinstance(item, Mapping):
                    raise ValueError("each friend request must be a mapping")
                external_id = self._require_text(
                    str(item.get("external_request_id", "")),
                    "external_request_id",
                )
                requester_external_id = self._require_text(
                    str(item.get("requester_external_id", "")),
                    "requester_external_id",
                )
                supplied_status = item.get("status")
                status = self._require_choice(
                    str(supplied_status or "pending"),
                    "status",
                    FRIEND_REQUEST_STATUSES,
                )
                requested_at = self._optional_datetime(
                    item.get("requested_at"), "requested_at", observed
                )
                raw_json = item.get("raw_json")
                if raw_json is not None and not isinstance(raw_json, Mapping):
                    raise ValueError("friend request raw_json must be a mapping")
                request = session.scalar(
                    select(FriendRequest).where(
                        FriendRequest.tenant_id == tenant_id,
                        FriendRequest.channel_account_id == channel_account_id,
                        FriendRequest.external_request_id == external_id,
                    )
                )
                if request is None:
                    request = FriendRequest(
                        tenant_id=tenant_id,
                        channel_account_id=channel_account_id,
                        external_request_id=external_id,
                        requester_external_id=requester_external_id,
                        display_name=str(item.get("display_name", "")).strip(),
                        verification_message=str(
                            item.get("verification_message", "")
                        ).strip(),
                        status=status,
                        raw_json=dict(raw_json or {}),
                        requested_at=requested_at,
                        first_observed_at=observed,
                        last_observed_at=observed,
                        decided_at=observed if status != "pending" else None,
                        created_at=observed,
                        updated_at=observed,
                    )
                    session.add(request)
                    session.flush()
                    created_count += 1
                else:
                    request.requester_external_id = requester_external_id
                    if "display_name" in item:
                        request.display_name = str(item.get("display_name", "")).strip()
                    if "verification_message" in item:
                        request.verification_message = str(
                            item.get("verification_message", "")
                        ).strip()
                    if raw_json is not None:
                        request.raw_json = dict(raw_json)
                    if requested_at < request.requested_at:
                        request.requested_at = requested_at
                    if observed > request.last_observed_at:
                        request.last_observed_at = observed
                    if request.status == "pending" and status != "pending":
                        request.status = status
                        request.decided_at = observed
                    request.updated_at = observed
                    updated_count += 1
                changed[request.id] = request
            if changed:
                self._append_audit_event(
                    session,
                    tenant_id,
                    action="friend_requests.synced",
                    entity_type="channel_account",
                    entity_id=channel_account_id,
                    details={"created": created_count, "updated": updated_count},
                    created_at=observed,
                )
                session.flush()
            return sorted(
                changed.values(),
                key=lambda item: (item.requested_at, item.id),
            )

    def list_friend_requests(
        self,
        tenant_id: str,
        channel_account_id: str | None = None,
        status: str | None = None,
        query: str = "",
        limit: int = 100,
    ) -> list[FriendRequest]:
        checked_limit = self._validate_management_limit(limit)
        if status is not None:
            status = self._require_choice(status, "status", FRIEND_REQUEST_STATUSES)
        normalized_query = query.strip()
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            statement = select(FriendRequest).where(FriendRequest.tenant_id == tenant_id)
            if channel_account_id is not None:
                self._require_channel_account(session, tenant_id, channel_account_id)
                statement = statement.where(
                    FriendRequest.channel_account_id == channel_account_id
                )
            if status is not None:
                statement = statement.where(FriendRequest.status == status)
            if normalized_query:
                statement = statement.where(
                    or_(
                        FriendRequest.external_request_id.icontains(
                            normalized_query, autoescape=True
                        ),
                        FriendRequest.requester_external_id.icontains(
                            normalized_query, autoescape=True
                        ),
                        FriendRequest.display_name.icontains(
                            normalized_query, autoescape=True
                        ),
                        FriendRequest.verification_message.icontains(
                            normalized_query, autoescape=True
                        ),
                    )
                )
            return list(
                session.scalars(
                    statement.order_by(FriendRequest.requested_at, FriendRequest.id).limit(
                        checked_limit
                    )
                )
            )

    @staticmethod
    def _operation_limit_values(
        *,
        batch_size: int | None,
        min_interval_seconds: int | None,
        max_per_hour: int | None,
        max_per_day: int | None,
        paused: bool | None,
        welcome_message: str | None,
        auto_enable_reply: bool | None,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        bounds = {
            "batch_size": (batch_size, 1, 100),
            "min_interval_seconds": (min_interval_seconds, 1, 3600),
            "max_per_hour": (max_per_hour, 1, 10000),
            "max_per_day": (max_per_day, 1, 100000),
        }
        for field, (value, minimum, maximum) in bounds.items():
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{field} must be an integer")
            if not minimum <= value <= maximum:
                raise ValueError(f"{field} must be between {minimum} and {maximum}")
            values[field] = value
        if paused is not None:
            values["paused"] = TenantStore._require_bool(paused, "paused")
        if welcome_message is not None:
            if not isinstance(welcome_message, str):
                raise ValueError("welcome_message must be a string")
            normalized_welcome = welcome_message.strip()
            if len(normalized_welcome) > 4000:
                raise ValueError("welcome_message cannot exceed 4000 characters")
            values["welcome_message"] = normalized_welcome
        if auto_enable_reply is not None:
            values["auto_enable_reply"] = TenantStore._require_bool(
                auto_enable_reply, "auto_enable_reply"
            )
        return values

    @staticmethod
    def _get_or_create_operation_limit(
        session: Session,
        tenant_id: str,
        channel_account_id: str,
        *,
        lock: bool = False,
    ) -> OperationLimit:
        statement = select(OperationLimit).where(
            OperationLimit.tenant_id == tenant_id,
            OperationLimit.channel_account_id == channel_account_id,
        )
        if lock:
            statement = statement.with_for_update()
        limits = session.scalar(statement)
        if limits is None:
            limits = OperationLimit(
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                batch_size=10,
                min_interval_seconds=5,
                max_per_hour=30,
                max_per_day=100,
                paused=False,
                welcome_message="",
                auto_enable_reply=False,
            )
            session.add(limits)
            session.flush()
        return limits

    def get_operation_limit(
        self, tenant_id: str, channel_account_id: str
    ) -> OperationLimit:
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_channel_account(session, tenant_id, channel_account_id)
            return self._get_or_create_operation_limit(
                session, tenant_id, channel_account_id
            )

    def update_operation_limit(
        self,
        tenant_id: str,
        channel_account_id: str,
        *,
        batch_size: int | None = None,
        min_interval_seconds: int | None = None,
        max_per_hour: int | None = None,
        max_per_day: int | None = None,
        paused: bool | None = None,
        welcome_message: str | None = None,
        auto_enable_reply: bool | None = None,
        actor_user_id: str,
    ) -> OperationLimit:
        values = self._operation_limit_values(
            batch_size=batch_size,
            min_interval_seconds=min_interval_seconds,
            max_per_hour=max_per_hour,
            max_per_day=max_per_day,
            paused=paused,
            welcome_message=welcome_message,
            auto_enable_reply=auto_enable_reply,
        )
        if not values:
            raise ValueError("at least one operation limit value must be supplied")
        changed_at = utc_now()
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_channel_account(session, tenant_id, channel_account_id)
            self._require_member(session, tenant_id, actor_user_id)
            limits = self._get_or_create_operation_limit(
                session,
                tenant_id,
                channel_account_id,
                lock=self._is_postgresql,
            )
            for field, value in values.items():
                setattr(limits, field, value)
            limits.updated_by_user_id = actor_user_id
            limits.updated_at = changed_at
            self._append_audit_event(
                session,
                tenant_id,
                actor_user_id=actor_user_id,
                action="operation_limit.updated",
                entity_type="channel_account",
                entity_id=channel_account_id,
                details=values,
                created_at=changed_at,
            )
            session.flush()
            return limits

    @staticmethod
    def _operation_idempotency_key(
        base_key: str, action: str, friend_request_id: str
    ) -> str:
        suffix = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{base_key}\x1f{action}\x1f{friend_request_id}",
            )
        )
        return f"{base_key[:210]}:{suffix}"

    def enqueue_friend_request_operations(
        self,
        tenant_id: str,
        channel_account_id: str,
        *,
        request_ids: Iterable[str] | None = None,
        selection: str,
        action: str,
        actor_user_id: str,
        idempotency_key: str,
        welcome_message: str | None = None,
        auto_enable_reply: bool | None = None,
        limit: int | None = None,
    ) -> list[OperationJob]:
        if selection not in {"selected", "batch", "all"}:
            raise ValueError("selection must be selected, batch, or all")
        if action not in {"accept_friend", "reject_friend"}:
            raise ValueError("action must be accept_friend or reject_friend")
        normalized_key = self._require_text(idempotency_key, "idempotency_key")
        if len(normalized_key) > 255:
            raise ValueError("idempotency_key cannot exceed 255 characters")
        if welcome_message is not None and not isinstance(welcome_message, str):
            raise ValueError("welcome_message must be a string")
        normalized_welcome = welcome_message.strip() if welcome_message is not None else None
        if normalized_welcome is not None and len(normalized_welcome) > 4000:
            raise ValueError("welcome_message cannot exceed 4000 characters")
        checked_auto_reply = (
            None
            if auto_enable_reply is None
            else self._require_bool(auto_enable_reply, "auto_enable_reply")
        )
        if action == "reject_friend" and (
            (normalized_welcome is not None and normalized_welcome)
            or checked_auto_reply is True
        ):
            raise ValueError("reject_friend cannot send a welcome or enable replies")
        if limit is not None:
            checked_limit = self._validate_management_limit(limit)
        else:
            checked_limit = None
        if isinstance(request_ids, (str, bytes)):
            raise ValueError("request_ids must be an iterable of IDs")
        selected_ids = (
            list(dict.fromkeys(str(item).strip() for item in request_ids))
            if request_ids is not None
            else []
        )
        if any(not item for item in selected_ids):
            raise ValueError("request_ids cannot contain empty IDs")
        if selection == "selected" and not selected_ids:
            raise ValueError("selected operations require request_ids")
        if selection != "selected" and selected_ids:
            raise ValueError("request_ids are only valid with selected selection")
        enqueued_at = utc_now()
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_channel_account(session, tenant_id, channel_account_id)
            self._require_member(session, tenant_id, actor_user_id)
            limits = self._get_or_create_operation_limit(
                session, tenant_id, channel_account_id
            )
            effective_welcome = (
                normalized_welcome
                if normalized_welcome is not None
                else limits.welcome_message
            ) if action == "accept_friend" else ""
            effective_auto_reply = (
                checked_auto_reply
                if checked_auto_reply is not None
                else limits.auto_enable_reply
            ) if action == "accept_friend" else False
            if selection == "selected":
                requests_to_enqueue = list(
                    session.scalars(
                        select(FriendRequest).where(
                            FriendRequest.tenant_id == tenant_id,
                            FriendRequest.channel_account_id == channel_account_id,
                            FriendRequest.id.in_(selected_ids),
                        )
                    )
                )
                by_id = {item.id: item for item in requests_to_enqueue}
                if len(by_id) != len(selected_ids):
                    raise LookupError("one or more friend requests were not found for tenant")
                requests_to_enqueue = [by_id[item] for item in selected_ids]
            else:
                statement = (
                    select(FriendRequest)
                    .where(
                        FriendRequest.tenant_id == tenant_id,
                        FriendRequest.channel_account_id == channel_account_id,
                        FriendRequest.status == "pending",
                    )
                    .order_by(FriendRequest.requested_at, FriendRequest.id)
                )
                if selection == "batch":
                    statement = statement.limit(checked_limit or limits.batch_size)
                elif checked_limit is not None:
                    statement = statement.limit(checked_limit)
                requests_to_enqueue = list(session.scalars(statement))

            if effective_welcome:
                missing_conversation = [
                    request.id for request in requests_to_enqueue if not request.display_name.strip()
                ]
                if missing_conversation:
                    raise ValueError(
                        "welcome_message requires a non-empty friend request display_name "
                        "for conversation_external_id"
                    )

            jobs: list[OperationJob] = []
            for request in requests_to_enqueue:
                job_key = self._operation_idempotency_key(
                    normalized_key, action, request.id
                )
                existing = session.scalar(
                    select(OperationJob).where(
                        OperationJob.tenant_id == tenant_id,
                        OperationJob.idempotency_key == job_key,
                    )
                )
                if existing is not None:
                    if (
                        existing.channel_account_id != channel_account_id
                        or existing.friend_request_id != request.id
                        or existing.operation_type != action
                    ):
                        raise ValueError("idempotency_key was already used for another operation")
                    jobs.append(existing)
                    continue
                existing_for_request = session.scalar(
                    select(OperationJob).where(
                        OperationJob.tenant_id == tenant_id,
                        OperationJob.friend_request_id == request.id,
                        OperationJob.operation_type == action,
                    )
                )
                if existing_for_request is not None:
                    jobs.append(existing_for_request)
                    continue
                conflicting = session.scalar(
                    select(OperationJob).where(
                        OperationJob.tenant_id == tenant_id,
                        OperationJob.friend_request_id == request.id,
                        OperationJob.operation_type.in_(("accept_friend", "reject_friend")),
                        OperationJob.operation_type != action,
                        OperationJob.status.in_(("queued", "claimed", "executing", "uncertain")),
                    )
                )
                if conflicting is not None:
                    raise ValueError("friend request already has a conflicting operation")
                if request.status != "pending":
                    raise ValueError("only pending friend requests can be enqueued")
                job = OperationJob(
                    tenant_id=tenant_id,
                    channel_account_id=channel_account_id,
                    friend_request_id=request.id,
                    operation_type=action,
                    status="queued",
                    idempotency_key=job_key,
                    payload_json={
                        "external_request_id": request.external_request_id,
                        "requester_external_id": request.requester_external_id,
                        "display_name": request.display_name,
                        "welcome_message": effective_welcome,
                        "auto_enable_reply": effective_auto_reply,
                    },
                    actor_user_id=actor_user_id,
                    available_at=enqueued_at,
                    created_at=enqueued_at,
                    updated_at=enqueued_at,
                )
                session.add(job)
                session.flush()
                jobs.append(job)
            if jobs:
                self._append_audit_event(
                    session,
                    tenant_id,
                    actor_user_id=actor_user_id,
                    action="operation.enqueued",
                    entity_type="channel_account",
                    entity_id=channel_account_id,
                    details={
                        "selection": selection,
                        "operation_type": action,
                        "job_ids": [item.id for item in jobs],
                    },
                    created_at=enqueued_at,
                )
                session.flush()
            return jobs

    @staticmethod
    def _operation_claim_candidates_statement(
        tenant_id: str,
        channel_account_id: str,
        now: datetime,
        limit: int,
        *,
        use_skip_locked: bool,
    ) -> Any:
        completed_dependencies = select(OperationJob.id).where(
            OperationJob.tenant_id == tenant_id,
            OperationJob.status == "succeeded",
        )
        statement = (
            select(OperationJob.id)
            .where(
                OperationJob.tenant_id == tenant_id,
                OperationJob.channel_account_id == channel_account_id,
                OperationJob.status == "queued",
                OperationJob.available_at <= now,
                or_(
                    OperationJob.depends_on_job_id.is_(None),
                    OperationJob.depends_on_job_id.in_(completed_dependencies),
                ),
            )
            .order_by(OperationJob.created_at, OperationJob.id)
            .limit(limit)
        )
        if use_skip_locked:
            statement = statement.with_for_update(skip_locked=True)
        return statement

    @staticmethod
    def _operation_attempt(
        session: Session, job: OperationJob, attempt_number: int
    ) -> OperationAttempt:
        attempt = session.scalar(
            select(OperationAttempt).where(
                OperationAttempt.tenant_id == job.tenant_id,
                OperationAttempt.operation_job_id == job.id,
                OperationAttempt.attempt_number == attempt_number,
            )
        )
        if attempt is None:
            raise RuntimeError("operation attempt record is missing")
        return attempt

    def _expire_operation_leases(
        self,
        session: Session,
        tenant_id: str,
        channel_account_id: str,
        now: datetime,
    ) -> None:
        statement = (
            select(OperationJob)
            .where(
                    OperationJob.tenant_id == tenant_id,
                    OperationJob.channel_account_id == channel_account_id,
                    OperationJob.status.in_(("claimed", "executing")),
                    OperationJob.lease_expires_at.is_not(None),
                    OperationJob.lease_expires_at <= now,
                )
            .order_by(OperationJob.created_at, OperationJob.id)
        )
        if self._is_postgresql:
            statement = statement.with_for_update(skip_locked=True)
        expired = list(session.scalars(statement))
        for job in expired:
            attempt = self._operation_attempt(session, job, job.attempts)
            attempt.completed_at = now
            if job.status == "executing":
                job.status = "uncertain"
                job.completed_at = now
                job.error = "operation lease expired while executing"
                attempt.status = "uncertain"
                attempt.error = job.error
                self._append_audit_event(
                    session,
                    tenant_id,
                    action="operation.lease_expired_uncertain",
                    entity_type="operation_job",
                    entity_id=job.id,
                    details={"attempt": job.attempts},
                    created_at=now,
                )
            else:
                job.status = "queued"
                job.available_at = now
                job.claimed_by_agent_id = None
                job.claimed_at = None
                job.error = "operation lease expired before execution"
                attempt.status = "failed"
                attempt.error = job.error
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = now

    def claim_operation_jobs(
        self,
        tenant_id: str,
        agent_id: str,
        channel_account_id: str,
        *,
        limit: int | None = None,
        lease_seconds: int = 120,
        now: datetime | None = None,
    ) -> list[OperationJob]:
        if limit is not None:
            checked_limit = self._validate_management_limit(limit)
        else:
            checked_limit = None
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool):
            raise ValueError("lease_seconds must be an integer")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        claimed_at = _as_utc(now or utc_now())
        lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_channel_account(session, tenant_id, channel_account_id)
            agent = self._require_agent(session, tenant_id, agent_id)
            if agent.channel_account_id != channel_account_id:
                raise ValueError("agent is not bound to the requested channel account")
            limits = self._get_or_create_operation_limit(
                session,
                tenant_id,
                channel_account_id,
                lock=self._is_postgresql,
            )
            self._expire_operation_leases(
                session, tenant_id, channel_account_id, claimed_at
            )
            if limits.paused:
                return []

            hour_start = claimed_at - timedelta(hours=1)
            day_start = claimed_at.replace(hour=0, minute=0, second=0, microsecond=0)
            hourly_count = int(
                session.scalar(
                    select(func.count(OperationAttempt.id)).where(
                        OperationAttempt.tenant_id == tenant_id,
                        OperationAttempt.channel_account_id == channel_account_id,
                        OperationAttempt.claimed_at >= hour_start,
                    )
                )
                or 0
            )
            daily_count = int(
                session.scalar(
                    select(func.count(OperationAttempt.id)).where(
                        OperationAttempt.tenant_id == tenant_id,
                        OperationAttempt.channel_account_id == channel_account_id,
                        OperationAttempt.claimed_at >= day_start,
                    )
                )
                or 0
            )
            remaining = min(
                limits.max_per_hour - hourly_count,
                limits.max_per_day - daily_count,
            )
            if remaining <= 0:
                return []
            latest_claimed_at = session.scalar(
                select(func.max(OperationAttempt.claimed_at)).where(
                    OperationAttempt.tenant_id == tenant_id,
                    OperationAttempt.channel_account_id == channel_account_id,
                )
            )
            if latest_claimed_at is not None:
                latest_claimed_at = _as_utc(latest_claimed_at)
                if latest_claimed_at + timedelta(
                    seconds=limits.min_interval_seconds
                ) > claimed_at:
                    return []

            claim_count = min(checked_limit or limits.batch_size, remaining)
            if limits.min_interval_seconds > 0:
                claim_count = min(claim_count, 1)
            candidate_ids = list(
                session.scalars(
                    self._operation_claim_candidates_statement(
                        tenant_id,
                        channel_account_id,
                        claimed_at,
                        claim_count,
                        use_skip_locked=self._is_postgresql,
                    )
                )
            )
            claimed_ids: list[str] = []
            for job_id in candidate_ids:
                lease_token = new_uuid()
                result = session.execute(
                    update(OperationJob)
                    .where(
                        OperationJob.id == job_id,
                        OperationJob.tenant_id == tenant_id,
                        OperationJob.channel_account_id == channel_account_id,
                        OperationJob.status == "queued",
                        OperationJob.available_at <= claimed_at,
                    )
                    .values(
                        status="claimed",
                        claimed_by_agent_id=agent_id,
                        lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                        attempts=OperationJob.attempts + 1,
                        claimed_at=claimed_at,
                        started_at=None,
                        completed_at=None,
                        error="",
                        resolution_key=None,
                        updated_at=claimed_at,
                    )
                )
                if result.rowcount != 1:
                    continue
                job = self._require_operation_job(session, tenant_id, job_id)
                session.add(
                    OperationAttempt(
                        tenant_id=tenant_id,
                        channel_account_id=channel_account_id,
                        operation_job_id=job.id,
                        agent_id=agent_id,
                        attempt_number=job.attempts,
                        lease_token=lease_token,
                        status="claimed",
                        claimed_at=claimed_at,
                    )
                )
                claimed_ids.append(job_id)
            if not claimed_ids:
                return []
            self._append_audit_event(
                session,
                tenant_id,
                action="operation.claimed",
                entity_type="agent_instance",
                entity_id=agent_id,
                details={"job_ids": claimed_ids},
                created_at=claimed_at,
            )
            session.flush()
            return list(
                session.scalars(
                    select(OperationJob)
                    .where(
                        OperationJob.tenant_id == tenant_id,
                        OperationJob.id.in_(claimed_ids),
                    )
                    .order_by(OperationJob.created_at, OperationJob.id)
                )
            )

    def start_operation_job(
        self,
        tenant_id: str,
        operation_job_id: str,
        agent_id: str,
        lease_token: str,
        attempt: int,
        *,
        now: datetime | None = None,
    ) -> OperationJob:
        started_at = _as_utc(now or utc_now())
        normalized_lease = self._require_text(lease_token, "lease_token")
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            agent = self._require_agent(session, tenant_id, agent_id)
            job = self._require_operation_job(
                session,
                tenant_id,
                operation_job_id,
                lock=self._is_postgresql,
            )
            if agent.channel_account_id != job.channel_account_id:
                raise ValueError("agent is not bound to the operation channel account")
            if job.status == "executing":
                if (
                    job.claimed_by_agent_id == agent_id
                    and job.lease_token == normalized_lease
                    and job.attempts == attempt
                ):
                    return job
                raise ValueError("operation job is executing under another lease")
            if job.status != "claimed":
                raise ValueError("operation job is not claimed")
            if job.claimed_by_agent_id != agent_id:
                raise ValueError("operation job is claimed by another agent")
            if job.lease_token != normalized_lease or job.attempts != attempt:
                raise ValueError("operation lease does not match")
            if job.lease_expires_at is None or job.lease_expires_at <= started_at:
                raise ValueError("operation lease has expired")
            result = session.execute(
                update(OperationJob)
                .where(
                    OperationJob.id == operation_job_id,
                    OperationJob.tenant_id == tenant_id,
                    OperationJob.status == "claimed",
                    OperationJob.claimed_by_agent_id == agent_id,
                    OperationJob.lease_token == normalized_lease,
                    OperationJob.attempts == attempt,
                    OperationJob.lease_expires_at > started_at,
                )
                .values(
                    status="executing",
                    started_at=started_at,
                    updated_at=started_at,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("operation start lost its lease")
            attempt_record = self._operation_attempt(session, job, attempt)
            attempt_record.status = "executing"
            attempt_record.started_at = started_at
            self._append_audit_event(
                session,
                tenant_id,
                action="operation.started",
                entity_type="operation_job",
                entity_id=job.id,
                details={"agent_id": agent_id, "attempt": attempt},
                created_at=started_at,
            )
            session.flush()
            return job

    @staticmethod
    def _apply_succeeded_operation(
        session: Session, job: OperationJob, completed_at: datetime
    ) -> None:
        if job.friend_request_id is None:
            return
        request = session.scalar(
            select(FriendRequest).where(
                FriendRequest.tenant_id == job.tenant_id,
                FriendRequest.id == job.friend_request_id,
            )
        )
        if request is None:
            raise RuntimeError("operation job lost its friend request")
        if job.operation_type == "reject_friend":
            request.status = "rejected"
            request.decided_at = completed_at
            request.updated_at = completed_at
            return
        if job.operation_type != "accept_friend":
            return
        request.status = "approved"
        request.decided_at = completed_at
        request.updated_at = completed_at
        contact = session.scalar(
            select(ChannelContact).where(
                ChannelContact.tenant_id == job.tenant_id,
                ChannelContact.channel_account_id == job.channel_account_id,
                ChannelContact.external_contact_id == request.requester_external_id,
            )
        )
        if contact is None:
            contact = ChannelContact(
                tenant_id=job.tenant_id,
                channel_account_id=job.channel_account_id,
                external_contact_id=request.requester_external_id,
                kind="friend",
                display_name=request.display_name,
                remark="",
                status="active",
                metadata_json={"friend_request_id": request.id},
                first_observed_at=completed_at,
                last_observed_at=completed_at,
                created_at=completed_at,
                updated_at=completed_at,
            )
            session.add(contact)
            session.flush()
        else:
            contact.kind = "friend"
            contact.status = "active"
            contact.last_observed_at = completed_at
            contact.updated_at = completed_at
        auto_enable = bool(job.payload_json.get("auto_enable_reply"))
        if auto_enable:
            policy = session.scalar(
                select(ChatPolicy).where(
                    ChatPolicy.tenant_id == job.tenant_id,
                    ChatPolicy.contact_id == contact.id,
                )
            )
            if policy is None:
                policy = ChatPolicy(
                    tenant_id=job.tenant_id,
                    contact_id=contact.id,
                    mode="ai_auto",
                    welcome_enabled=bool(job.payload_json.get("welcome_message")),
                    created_at=completed_at,
                    updated_at=completed_at,
                )
                session.add(policy)
            else:
                policy.mode = "ai_auto"
                policy.welcome_enabled = bool(job.payload_json.get("welcome_message"))
                policy.updated_at = completed_at
        welcome_message = str(job.payload_json.get("welcome_message", "")).strip()
        if welcome_message:
            conversation_external_id = request.display_name.strip()
            if not conversation_external_id:
                raise ValueError(
                    "send_welcome requires a non-empty friend request display_name"
                )
            welcome_key = f"{job.idempotency_key[:242]}:welcome"
            existing_welcome = session.scalar(
                select(OperationJob).where(
                    OperationJob.tenant_id == job.tenant_id,
                    OperationJob.idempotency_key == welcome_key,
                )
            )
            if existing_welcome is None:
                session.add(
                    OperationJob(
                        tenant_id=job.tenant_id,
                        channel_account_id=job.channel_account_id,
                        friend_request_id=request.id,
                        depends_on_job_id=job.id,
                        operation_type="send_welcome",
                        status="queued",
                        idempotency_key=welcome_key,
                        payload_json={
                            "external_contact_id": request.requester_external_id,
                            "conversation_external_id": conversation_external_id,
                            "content": welcome_message,
                        },
                        actor_user_id=job.actor_user_id,
                        available_at=completed_at,
                        created_at=completed_at,
                        updated_at=completed_at,
                    )
                )

    def complete_operation_job(
        self,
        tenant_id: str,
        operation_job_id: str,
        agent_id: str,
        lease_token: str,
        attempt: int,
        status: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: str = "",
        now: datetime | None = None,
    ) -> OperationJob:
        if status not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("status must be succeeded, failed, or uncertain")
        if result is not None and not isinstance(result, Mapping):
            raise ValueError("result must be a mapping")
        completed_at = _as_utc(now or utc_now())
        normalized_lease = self._require_text(lease_token, "lease_token")
        normalized_error = str(error).strip()[:2000]
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            agent = self._require_agent(session, tenant_id, agent_id)
            job = self._require_operation_job(
                session,
                tenant_id,
                operation_job_id,
                lock=self._is_postgresql,
            )
            if agent.channel_account_id != job.channel_account_id:
                raise ValueError("agent is not bound to the operation channel account")
            attempt_record = self._operation_attempt(session, job, attempt)
            matching_attempt = (
                job.attempts == attempt
                and attempt_record.agent_id == agent_id
                and attempt_record.lease_token == normalized_lease
            )
            if job.status in {"succeeded", "failed", "uncertain"}:
                if (
                    job.status == status
                    and matching_attempt
                ):
                    return job
                if job.resolution_key is not None and matching_attempt:
                    return job
                if not (
                    job.status == "uncertain"
                    and job.resolution_key is None
                    and status in {"succeeded", "failed"}
                    and matching_attempt
                ):
                    raise ValueError("operation job is already terminal")
            elif job.status != "executing":
                raise ValueError("operation job is not executing")
            if not matching_attempt:
                raise ValueError("operation lease does not match")
            if job.status == "executing" and job.claimed_by_agent_id != agent_id:
                raise ValueError("operation job is claimed by another agent")
            job.status = status
            job.completed_at = completed_at
            job.lease_token = None
            job.lease_expires_at = None
            job.error = normalized_error
            job.updated_at = completed_at
            attempt_record.status = status
            attempt_record.result_json = dict(result or {})
            attempt_record.error = normalized_error
            attempt_record.completed_at = completed_at
            if status == "succeeded":
                self._apply_succeeded_operation(session, job, completed_at)
            self._append_audit_event(
                session,
                tenant_id,
                action=f"operation.{status}",
                entity_type="operation_job",
                entity_id=job.id,
                details={"agent_id": agent_id, "attempt": attempt, "error": normalized_error},
                created_at=completed_at,
            )
            session.flush()
            return job

    def list_operation_jobs(
        self,
        tenant_id: str,
        channel_account_id: str | None = None,
        status: str | None = None,
        operation_type: str | None = None,
        limit: int = 100,
    ) -> list[OperationJob]:
        checked_limit = self._validate_management_limit(limit)
        if status is not None:
            status = self._require_choice(status, "status", OPERATION_STATUSES)
        if operation_type is not None:
            operation_type = self._require_choice(
                operation_type, "operation_type", OPERATION_TYPES
            )
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            statement = select(OperationJob).where(OperationJob.tenant_id == tenant_id)
            if channel_account_id is not None:
                self._require_channel_account(session, tenant_id, channel_account_id)
                statement = statement.where(
                    OperationJob.channel_account_id == channel_account_id
                )
            if status is not None:
                statement = statement.where(OperationJob.status == status)
            if operation_type is not None:
                statement = statement.where(
                    OperationJob.operation_type == operation_type
                )
            return list(
                session.scalars(
                    statement.order_by(OperationJob.created_at, OperationJob.id).limit(
                        checked_limit
                    )
                )
            )

    def cancel_operation_job(
        self,
        tenant_id: str,
        operation_job_id: str,
        actor_user_id: str,
        *,
        now: datetime | None = None,
    ) -> OperationJob:
        cancelled_at = _as_utc(now or utc_now())
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_member(session, tenant_id, actor_user_id)
            job = self._require_operation_job(
                session,
                tenant_id,
                operation_job_id,
                lock=self._is_postgresql,
            )
            if job.status == "cancelled":
                return job
            if job.status != "queued":
                raise ValueError("only queued operation jobs can be cancelled")
            job.status = "cancelled"
            job.completed_at = cancelled_at
            job.updated_at = cancelled_at
            self._append_audit_event(
                session,
                tenant_id,
                actor_user_id=actor_user_id,
                action="operation.cancelled",
                entity_type="operation_job",
                entity_id=job.id,
                created_at=cancelled_at,
            )
            session.flush()
            return job

    def retry_operation_job(
        self,
        tenant_id: str,
        operation_job_id: str,
        actor_user_id: str,
        *,
        available_at: datetime | None = None,
    ) -> OperationJob:
        retried_at = utc_now()
        next_available_at = self._optional_datetime(
            available_at, "available_at", retried_at
        )
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_member(session, tenant_id, actor_user_id)
            job = self._require_operation_job(
                session,
                tenant_id,
                operation_job_id,
                lock=self._is_postgresql,
            )
            if job.status not in {"failed", "cancelled"}:
                raise ValueError("only failed or cancelled operation jobs can be retried")
            job.status = "queued"
            job.available_at = next_available_at
            job.claimed_by_agent_id = None
            job.lease_token = None
            job.lease_expires_at = None
            job.claimed_at = None
            job.started_at = None
            job.completed_at = None
            job.error = ""
            job.resolution_key = None
            job.updated_at = retried_at
            self._append_audit_event(
                session,
                tenant_id,
                actor_user_id=actor_user_id,
                action="operation.retried",
                entity_type="operation_job",
                entity_id=job.id,
                details={"available_at": next_available_at.isoformat()},
                created_at=retried_at,
            )
            session.flush()
            return job

    def resolve_operation_job(
        self,
        tenant_id: str,
        operation_job_id: str,
        resolution: str,
        idempotency_key: str,
        actor_user_id: str,
        note: str,
        *,
        now: datetime | None = None,
    ) -> OperationJob:
        if resolution not in {"succeeded", "failed", "retry", "cancelled"}:
            raise ValueError("resolution must be succeeded, failed, retry, or cancelled")
        normalized_key = self._require_text(idempotency_key, "idempotency_key")
        normalized_note = self._require_text(note, "note")[:2000]
        resolved_at = _as_utc(now or utc_now())
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            self._require_member(session, tenant_id, actor_user_id)
            job = self._require_operation_job(
                session,
                tenant_id,
                operation_job_id,
                lock=self._is_postgresql,
            )
            if job.resolution_key == normalized_key:
                return job
            if job.status != "uncertain":
                raise ValueError("only uncertain operation jobs can be resolved")
            if resolution == "retry":
                job.status = "queued"
                job.available_at = resolved_at
                job.claimed_by_agent_id = None
                job.claimed_at = None
                job.started_at = None
                job.completed_at = None
                job.error = ""
            else:
                job.status = resolution
                job.completed_at = resolved_at
                job.error = normalized_note if resolution == "failed" else ""
                if resolution == "succeeded":
                    self._apply_succeeded_operation(session, job, resolved_at)
            job.lease_token = None
            job.lease_expires_at = None
            job.resolution_key = normalized_key
            job.updated_at = resolved_at
            self._append_audit_event(
                session,
                tenant_id,
                actor_user_id=actor_user_id,
                action="operation.uncertain_resolved",
                entity_type="operation_job",
                entity_id=job.id,
                details={"resolution": resolution, "note": normalized_note},
                created_at=resolved_at,
            )
            session.flush()
            return job

    def list_messages(
        self,
        tenant_id: str,
        *,
        channel_account_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[Message]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            statement = select(Message).where(Message.tenant_id == tenant_id)
            if channel_account_id is not None:
                self._require_channel_account(session, tenant_id, channel_account_id)
                statement = statement.where(Message.channel_account_id == channel_account_id)
            if conversation_id is not None:
                self._require_conversation(session, tenant_id, conversation_id)
                statement = statement.where(Message.conversation_id == conversation_id)
            return list(
                session.scalars(statement.order_by(Message.received_at, Message.id).limit(limit))
            )

    def list_delivery_jobs(
        self,
        tenant_id: str,
        *,
        channel_account_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[DeliveryJob]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            statement = select(DeliveryJob).where(DeliveryJob.tenant_id == tenant_id)
            if channel_account_id is not None:
                self._require_channel_account(session, tenant_id, channel_account_id)
                statement = statement.where(
                    DeliveryJob.channel_account_id == channel_account_id
                )
            if status is not None:
                statement = statement.where(DeliveryJob.status == status)
            return list(
                session.scalars(
                    statement.order_by(DeliveryJob.created_at, DeliveryJob.id).limit(limit)
                )
            )

    def list_audit_events(self, tenant_id: str, *, limit: int = 100) -> list[AuditEvent]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return list(
                session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.tenant_id == tenant_id)
                    .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                    .limit(limit)
                )
            )

    def upsert_bot_profile(
        self,
        tenant_id: str,
        *,
        name: str = "default",
        prompt: str,
        settings: Mapping[str, Any] | None = None,
    ) -> BotProfile:
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            normalized_name = self._require_text(name, "name")
            profile = session.scalar(
                select(BotProfile).where(
                    BotProfile.tenant_id == tenant_id,
                    BotProfile.name == normalized_name,
                )
            )
            if profile is None:
                profile = BotProfile(
                    tenant_id=tenant_id,
                    name=normalized_name,
                    prompt=self._require_text(prompt, "prompt"),
                    settings_json=dict(settings or {}),
                )
                session.add(profile)
            else:
                profile.prompt = self._require_text(prompt, "prompt")
                profile.settings_json = dict(settings or {})
                profile.updated_at = utc_now()
            session.flush()
            return profile

    def get_bot_profile(
        self,
        tenant_id: str,
        *,
        name: str = "default",
    ) -> BotProfile | None:
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return session.scalar(
                select(BotProfile).where(
                    BotProfile.tenant_id == tenant_id,
                    BotProfile.name == name,
                )
            )

    def upsert_knowledge_base(
        self,
        tenant_id: str,
        *,
        name: str = "default",
        content: str,
        status: str = "active",
        config: Mapping[str, Any] | None = None,
    ) -> KnowledgeBase:
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            normalized_name = self._require_text(name, "name")
            knowledge = session.scalar(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.name == normalized_name,
                )
            )
            if knowledge is None:
                knowledge = KnowledgeBase(
                    tenant_id=tenant_id,
                    name=normalized_name,
                    status=self._require_text(status, "status"),
                    content=str(content),
                    config_json=dict(config or {}),
                )
                session.add(knowledge)
            else:
                knowledge.status = self._require_text(status, "status")
                knowledge.content = str(content)
                knowledge.config_json = dict(config or {})
                knowledge.updated_at = utc_now()
            session.flush()
            return knowledge

    def get_knowledge_base(
        self,
        tenant_id: str,
        *,
        name: str = "default",
    ) -> KnowledgeBase | None:
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return session.scalar(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.name == name,
                )
            )

    def store_api_credential(
        self,
        tenant_id: str,
        provider: str,
        name: str,
        encrypted_value: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> ApiCredential:
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            normalized_provider = self._require_text(provider, "provider")
            normalized_name = self._require_text(name, "name")
            credential = session.scalar(
                select(ApiCredential).where(
                    ApiCredential.tenant_id == tenant_id,
                    ApiCredential.provider == normalized_provider,
                    ApiCredential.name == normalized_name,
                )
            )
            now = utc_now()
            if credential is None:
                credential = ApiCredential(
                    tenant_id=tenant_id,
                    provider=normalized_provider,
                    name=normalized_name,
                    encrypted_value=self._require_text(encrypted_value, "encrypted_value"),
                    metadata_json=dict(metadata or {}),
                )
                session.add(credential)
            else:
                credential.encrypted_value = self._require_text(
                    encrypted_value, "encrypted_value"
                )
                credential.metadata_json = dict(metadata or {})
                credential.updated_at = now
            session.flush()
            return credential

    def get_api_credential(
        self, tenant_id: str, provider: str, name: str
    ) -> ApiCredential | None:
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return session.scalar(
                select(ApiCredential).where(
                    ApiCredential.tenant_id == tenant_id,
                    ApiCredential.provider == provider,
                    ApiCredential.name == name,
                )
            )

    def store_tenant_secret(
        self, tenant_id: str, name: str, encrypted_value: str
    ) -> TenantSecret:
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            normalized_name = self._require_text(name, "name")
            secret = session.scalar(
                select(TenantSecret).where(
                    TenantSecret.tenant_id == tenant_id,
                    TenantSecret.name == normalized_name,
                )
            )
            if secret is None:
                secret = TenantSecret(
                    tenant_id=tenant_id,
                    name=normalized_name,
                    encrypted_value=self._require_text(encrypted_value, "encrypted_value"),
                )
                session.add(secret)
            else:
                secret.encrypted_value = self._require_text(
                    encrypted_value, "encrypted_value"
                )
                secret.updated_at = utc_now()
            session.flush()
            return secret

    def get_tenant_secret(self, tenant_id: str, name: str) -> TenantSecret | None:
        with self._read_session() as session:
            self._require_tenant(session, tenant_id)
            return session.scalar(
                select(TenantSecret).where(
                    TenantSecret.tenant_id == tenant_id,
                    TenantSecret.name == name,
                )
            )

    def append_audit(
        self,
        tenant_id: str,
        action: str,
        *,
        actor_user_id: str | None = None,
        entity_type: str = "",
        entity_id: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        with self._transaction() as session:
            self._require_tenant(session, tenant_id)
            if actor_user_id is not None:
                membership = session.scalar(
                    select(Membership).where(
                        Membership.tenant_id == tenant_id,
                        Membership.user_id == actor_user_id,
                    )
                )
                if membership is None:
                    raise ValueError("audit actor is not a member of tenant")
            audit = AuditEvent(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=self._require_text(action, "action"),
                entity_type=entity_type.strip(),
                entity_id=entity_id.strip(),
                details_json=dict(details or {}),
            )
            session.add(audit)
            session.flush()
            return audit


__all__ = [
    "AccessCredential",
    "AgentActivation",
    "AgentInstance",
    "ApiCredential",
    "AuditEvent",
    "Base",
    "BotProfile",
    "ChannelAccount",
    "ChannelContact",
    "ChatPolicy",
    "Conversation",
    "DeliveryJob",
    "FriendRequest",
    "KnowledgeBase",
    "Membership",
    "Message",
    "OperationAttempt",
    "OperationJob",
    "OperationLimit",
    "ReplyJob",
    "Tenant",
    "TenantSecret",
    "TenantStore",
    "User",
    "utc_now",
]
