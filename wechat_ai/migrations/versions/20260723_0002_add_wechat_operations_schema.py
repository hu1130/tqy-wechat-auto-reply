"""add wechat operations schema

Revision ID: 20260723_0002
Revises: 20260723_0001
Create Date: 2026-07-23 10:42:55.584042
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '20260723_0002'
down_revision: Union[str, Sequence[str], None] = '20260723_0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('channel_contacts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('channel_account_id', sa.String(length=36), nullable=False),
    sa.Column('external_contact_id', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('remark', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('metadata_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('first_observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('friend', 'group')", name='ck_channel_contact_kind'),
    sa.CheckConstraint("status IN ('active', 'inactive', 'deleted')", name='ck_channel_contact_status'),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'channel_account_id', 'external_contact_id', name='uq_channel_contact_external')
    )
    op.create_index('ix_channel_contact_list', 'channel_contacts', ['tenant_id', 'channel_account_id', 'kind', 'status'], unique=False)
    op.create_index(op.f('ix_channel_contacts_channel_account_id'), 'channel_contacts', ['channel_account_id'], unique=False)
    op.create_index(op.f('ix_channel_contacts_tenant_id'), 'channel_contacts', ['tenant_id'], unique=False)
    op.create_table('friend_requests',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('channel_account_id', sa.String(length=36), nullable=False),
    sa.Column('external_request_id', sa.String(length=255), nullable=False),
    sa.Column('requester_external_id', sa.String(length=255), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('verification_message', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('raw_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('first_observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'expired')", name='ck_friend_request_status'),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'channel_account_id', 'external_request_id', name='uq_friend_request_external')
    )
    op.create_index('ix_friend_request_list', 'friend_requests', ['tenant_id', 'channel_account_id', 'status', 'requested_at'], unique=False)
    op.create_index(op.f('ix_friend_requests_channel_account_id'), 'friend_requests', ['channel_account_id'], unique=False)
    op.create_index(op.f('ix_friend_requests_tenant_id'), 'friend_requests', ['tenant_id'], unique=False)
    op.create_table('operation_limits',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('channel_account_id', sa.String(length=36), nullable=False),
    sa.Column('batch_size', sa.Integer(), nullable=False),
    sa.Column('min_interval_seconds', sa.Integer(), nullable=False),
    sa.Column('max_per_hour', sa.Integer(), nullable=False),
    sa.Column('max_per_day', sa.Integer(), nullable=False),
    sa.Column('paused', sa.Boolean(), nullable=False),
    sa.Column('welcome_message', sa.Text(), nullable=False),
    sa.Column('auto_enable_reply', sa.Boolean(), nullable=False),
    sa.Column('updated_by_user_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('batch_size BETWEEN 1 AND 100', name='ck_operation_limit_batch'),
    sa.CheckConstraint('max_per_day BETWEEN 1 AND 100000', name='ck_operation_limit_day'),
    sa.CheckConstraint('max_per_hour BETWEEN 1 AND 10000', name='ck_operation_limit_hour'),
    sa.CheckConstraint('min_interval_seconds BETWEEN 1 AND 3600', name='ck_operation_limit_interval'),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['updated_by_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'channel_account_id', name='uq_operation_limit_tenant_channel')
    )
    op.create_index(op.f('ix_operation_limits_channel_account_id'), 'operation_limits', ['channel_account_id'], unique=False)
    op.create_index(op.f('ix_operation_limits_tenant_id'), 'operation_limits', ['tenant_id'], unique=False)
    op.create_table('chat_policies',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('contact_id', sa.String(length=36), nullable=False),
    sa.Column('mode', sa.String(length=32), nullable=False),
    sa.Column('welcome_enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("mode IN ('ai_auto', 'manual', 'paused', 'blocked')", name='ck_chat_policy_mode'),
    sa.ForeignKeyConstraint(['contact_id'], ['channel_contacts.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'contact_id', name='uq_chat_policy_tenant_contact')
    )
    op.create_index(op.f('ix_chat_policies_contact_id'), 'chat_policies', ['contact_id'], unique=False)
    op.create_index(op.f('ix_chat_policies_tenant_id'), 'chat_policies', ['tenant_id'], unique=False)
    op.create_table('operation_jobs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('channel_account_id', sa.String(length=36), nullable=False),
    sa.Column('friend_request_id', sa.String(length=36), nullable=True),
    sa.Column('depends_on_job_id', sa.String(length=36), nullable=True),
    sa.Column('operation_type', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('payload_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('actor_user_id', sa.String(length=36), nullable=True),
    sa.Column('claimed_by_agent_id', sa.String(length=36), nullable=True),
    sa.Column('lease_token', sa.String(length=36), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('resolution_key', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("operation_type IN ('accept_friend', 'reject_friend', 'send_welcome')", name='ck_operation_type'),
    sa.CheckConstraint("status IN ('queued', 'claimed', 'executing', 'succeeded', 'failed', 'uncertain', 'cancelled')", name='ck_operation_status'),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], ),
    sa.ForeignKeyConstraint(['claimed_by_agent_id'], ['agent_instances.id'], ),
    sa.ForeignKeyConstraint(['depends_on_job_id'], ['operation_jobs.id'], ),
    sa.ForeignKeyConstraint(['friend_request_id'], ['friend_requests.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'friend_request_id', 'operation_type', name='uq_operation_friend_request_type'),
    sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_operation_tenant_idempotency')
    )
    op.create_index('ix_operation_claim', 'operation_jobs', ['tenant_id', 'channel_account_id', 'status', 'available_at'], unique=False)
    op.create_index(op.f('ix_operation_jobs_channel_account_id'), 'operation_jobs', ['channel_account_id'], unique=False)
    op.create_index(op.f('ix_operation_jobs_friend_request_id'), 'operation_jobs', ['friend_request_id'], unique=False)
    op.create_index(op.f('ix_operation_jobs_tenant_id'), 'operation_jobs', ['tenant_id'], unique=False)
    op.create_table('operation_attempts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('channel_account_id', sa.String(length=36), nullable=False),
    sa.Column('operation_job_id', sa.String(length=36), nullable=False),
    sa.Column('agent_id', sa.String(length=36), nullable=False),
    sa.Column('attempt_number', sa.Integer(), nullable=False),
    sa.Column('lease_token', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('result_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('claimed', 'executing', 'succeeded', 'failed', 'uncertain')", name='ck_operation_attempt_status'),
    sa.ForeignKeyConstraint(['agent_id'], ['agent_instances.id'], ),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], ),
    sa.ForeignKeyConstraint(['operation_job_id'], ['operation_jobs.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'operation_job_id', 'attempt_number', name='uq_operation_attempt_number')
    )
    op.create_index('ix_operation_attempt_rate', 'operation_attempts', ['tenant_id', 'channel_account_id', 'claimed_at'], unique=False)
    op.create_index(op.f('ix_operation_attempts_channel_account_id'), 'operation_attempts', ['channel_account_id'], unique=False)
    op.create_index(op.f('ix_operation_attempts_operation_job_id'), 'operation_attempts', ['operation_job_id'], unique=False)
    op.create_index(op.f('ix_operation_attempts_tenant_id'), 'operation_attempts', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_operation_attempts_tenant_id'), table_name='operation_attempts')
    op.drop_index(op.f('ix_operation_attempts_operation_job_id'), table_name='operation_attempts')
    op.drop_index(op.f('ix_operation_attempts_channel_account_id'), table_name='operation_attempts')
    op.drop_index('ix_operation_attempt_rate', table_name='operation_attempts')
    op.drop_table('operation_attempts')
    op.drop_index(op.f('ix_operation_jobs_tenant_id'), table_name='operation_jobs')
    op.drop_index(op.f('ix_operation_jobs_friend_request_id'), table_name='operation_jobs')
    op.drop_index(op.f('ix_operation_jobs_channel_account_id'), table_name='operation_jobs')
    op.drop_index('ix_operation_claim', table_name='operation_jobs')
    op.drop_table('operation_jobs')
    op.drop_index(op.f('ix_chat_policies_tenant_id'), table_name='chat_policies')
    op.drop_index(op.f('ix_chat_policies_contact_id'), table_name='chat_policies')
    op.drop_table('chat_policies')
    op.drop_index(op.f('ix_operation_limits_tenant_id'), table_name='operation_limits')
    op.drop_index(op.f('ix_operation_limits_channel_account_id'), table_name='operation_limits')
    op.drop_table('operation_limits')
    op.drop_index(op.f('ix_friend_requests_tenant_id'), table_name='friend_requests')
    op.drop_index(op.f('ix_friend_requests_channel_account_id'), table_name='friend_requests')
    op.drop_index('ix_friend_request_list', table_name='friend_requests')
    op.drop_table('friend_requests')
    op.drop_index(op.f('ix_channel_contacts_tenant_id'), table_name='channel_contacts')
    op.drop_index(op.f('ix_channel_contacts_channel_account_id'), table_name='channel_contacts')
    op.drop_index('ix_channel_contact_list', table_name='channel_contacts')
    op.drop_table('channel_contacts')
