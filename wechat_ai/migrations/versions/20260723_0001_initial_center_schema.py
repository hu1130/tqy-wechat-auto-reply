"""initial center schema

Revision ID: 20260723_0001
Revises:
Create Date: 2026-07-23 10:42:55.033159
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '20260723_0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('tenants',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('users',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('access_credentials',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('subject_type', sa.String(length=16), nullable=False),
    sa.Column('subject_id', sa.String(length=36), nullable=False),
    sa.Column('key_id', sa.String(length=64), nullable=False),
    sa.Column('prefix', sa.String(length=32), nullable=False),
    sa.Column('key_hash', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("subject_type IN ('user', 'agent')", name='ck_access_subject_type'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'key_id', name='uq_access_credential_tenant_key')
    )
    op.create_index(op.f('ix_access_credentials_tenant_id'), 'access_credentials', ['tenant_id'], unique=False)
    op.create_table('api_credentials',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('provider', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('encrypted_value', sa.Text(), nullable=False),
    sa.Column('metadata_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'provider', 'name', name='uq_api_credential_tenant_name')
    )
    op.create_index(op.f('ix_api_credentials_tenant_id'), 'api_credentials', ['tenant_id'], unique=False)
    op.create_table('audit_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('actor_user_id', sa.String(length=36), nullable=True),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('entity_type', sa.String(length=100), nullable=False),
    sa.Column('entity_id', sa.String(length=36), nullable=False),
    sa.Column('details_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_events_tenant_id'), 'audit_events', ['tenant_id'], unique=False)
    op.create_index('ix_audit_tenant_created', 'audit_events', ['tenant_id', 'created_at'], unique=False)
    op.create_table('bot_profiles',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('settings_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_bot_profile_tenant_name')
    )
    op.create_index(op.f('ix_bot_profiles_tenant_id'), 'bot_profiles', ['tenant_id'], unique=False)
    op.create_table('channel_accounts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('channel', sa.String(length=64), nullable=False),
    sa.Column('external_account_id', sa.String(length=255), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('config_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'channel', 'external_account_id', name='uq_channel_account_external')
    )
    op.create_index(op.f('ix_channel_accounts_tenant_id'), 'channel_accounts', ['tenant_id'], unique=False)
    op.create_table('knowledge_bases',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('config_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_kb_tenant_name')
    )
    op.create_index(op.f('ix_knowledge_bases_tenant_id'), 'knowledge_bases', ['tenant_id'], unique=False)
    op.create_table('memberships',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('role', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'user_id', name='uq_membership_tenant_user')
    )
    op.create_index(op.f('ix_memberships_tenant_id'), 'memberships', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_memberships_user_id'), 'memberships', ['user_id'], unique=False)
    op.create_table('tenant_secrets',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('encrypted_value', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_tenant_secret_name')
    )
    op.create_index(op.f('ix_tenant_secrets_tenant_id'), 'tenant_secrets', ['tenant_id'], unique=False)
    op.create_table('agent_instances',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('channel_account_id', sa.String(length=36), nullable=False),
    sa.Column('external_agent_id', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('capabilities_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'external_agent_id', name='uq_agent_tenant_external')
    )
    op.create_index(op.f('ix_agent_instances_channel_account_id'), 'agent_instances', ['channel_account_id'], unique=False)
    op.create_index(op.f('ix_agent_instances_tenant_id'), 'agent_instances', ['tenant_id'], unique=False)
    op.create_table('conversations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('channel_account_id', sa.String(length=36), nullable=False),
    sa.Column('external_conversation_id', sa.String(length=255), nullable=False),
    sa.Column('customer_external_id', sa.String(length=255), nullable=False),
    sa.Column('customer_display_name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'channel_account_id', 'external_conversation_id', name='uq_conversation_external')
    )
    op.create_index(op.f('ix_conversations_channel_account_id'), 'conversations', ['channel_account_id'], unique=False)
    op.create_index(op.f('ix_conversations_tenant_id'), 'conversations', ['tenant_id'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('channel_account_id', sa.String(length=36), nullable=False),
    sa.Column('external_message_id', sa.String(length=255), nullable=False),
    sa.Column('direction', sa.String(length=16), nullable=False),
    sa.Column('message_type', sa.String(length=64), nullable=False),
    sa.Column('sender_external_id', sa.String(length=255), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('raw_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("direction IN ('inbound', 'outbound')", name='ck_message_direction'),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], ),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'channel_account_id', 'external_message_id', name='uq_message_external')
    )
    op.create_index(op.f('ix_messages_channel_account_id'), 'messages', ['channel_account_id'], unique=False)
    op.create_index('ix_messages_tenant_conversation_received', 'messages', ['tenant_id', 'conversation_id', 'received_at'], unique=False)
    op.create_index(op.f('ix_messages_tenant_id'), 'messages', ['tenant_id'], unique=False)
    op.create_table('delivery_jobs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('channel_account_id', sa.String(length=36), nullable=False),
    sa.Column('conversation_id', sa.String(length=36), nullable=True),
    sa.Column('source_message_id', sa.String(length=36), nullable=True),
    sa.Column('dedupe_key', sa.String(length=255), nullable=False),
    sa.Column('payload_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('claimed_by_agent_id', sa.String(length=36), nullable=True),
    sa.Column('lease_token', sa.String(length=36), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('resolution_key', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'claimed', 'sending', 'completed', 'failed', 'uncertain')", name='ck_delivery_status'),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], ),
    sa.ForeignKeyConstraint(['claimed_by_agent_id'], ['agent_instances.id'], ),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.ForeignKeyConstraint(['source_message_id'], ['messages.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'dedupe_key', name='uq_delivery_tenant_dedupe')
    )
    op.create_index('ix_delivery_claim', 'delivery_jobs', ['tenant_id', 'channel_account_id', 'status', 'available_at'], unique=False)
    op.create_index(op.f('ix_delivery_jobs_channel_account_id'), 'delivery_jobs', ['channel_account_id'], unique=False)
    op.create_index(op.f('ix_delivery_jobs_tenant_id'), 'delivery_jobs', ['tenant_id'], unique=False)
    op.create_table('reply_jobs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('source_message_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('lease_token', sa.String(length=36), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('generated_delivery_id', sa.String(length=36), nullable=True),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'processing', 'generated', 'failed')", name='ck_reply_job_status'),
    sa.ForeignKeyConstraint(['generated_delivery_id'], ['delivery_jobs.id'], ),
    sa.ForeignKeyConstraint(['source_message_id'], ['messages.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'source_message_id', name='uq_reply_job_source_message')
    )
    op.create_index('ix_reply_job_claim', 'reply_jobs', ['tenant_id', 'status', 'available_at'], unique=False)
    op.create_index(op.f('ix_reply_jobs_tenant_id'), 'reply_jobs', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_reply_jobs_tenant_id'), table_name='reply_jobs')
    op.drop_index('ix_reply_job_claim', table_name='reply_jobs')
    op.drop_table('reply_jobs')
    op.drop_index(op.f('ix_delivery_jobs_tenant_id'), table_name='delivery_jobs')
    op.drop_index(op.f('ix_delivery_jobs_channel_account_id'), table_name='delivery_jobs')
    op.drop_index('ix_delivery_claim', table_name='delivery_jobs')
    op.drop_table('delivery_jobs')
    op.drop_index(op.f('ix_messages_tenant_id'), table_name='messages')
    op.drop_index('ix_messages_tenant_conversation_received', table_name='messages')
    op.drop_index(op.f('ix_messages_channel_account_id'), table_name='messages')
    op.drop_table('messages')
    op.drop_index(op.f('ix_conversations_tenant_id'), table_name='conversations')
    op.drop_index(op.f('ix_conversations_channel_account_id'), table_name='conversations')
    op.drop_table('conversations')
    op.drop_index(op.f('ix_agent_instances_tenant_id'), table_name='agent_instances')
    op.drop_index(op.f('ix_agent_instances_channel_account_id'), table_name='agent_instances')
    op.drop_table('agent_instances')
    op.drop_index(op.f('ix_tenant_secrets_tenant_id'), table_name='tenant_secrets')
    op.drop_table('tenant_secrets')
    op.drop_index(op.f('ix_memberships_user_id'), table_name='memberships')
    op.drop_index(op.f('ix_memberships_tenant_id'), table_name='memberships')
    op.drop_table('memberships')
    op.drop_index(op.f('ix_knowledge_bases_tenant_id'), table_name='knowledge_bases')
    op.drop_table('knowledge_bases')
    op.drop_index(op.f('ix_channel_accounts_tenant_id'), table_name='channel_accounts')
    op.drop_table('channel_accounts')
    op.drop_index(op.f('ix_bot_profiles_tenant_id'), table_name='bot_profiles')
    op.drop_table('bot_profiles')
    op.drop_index('ix_audit_tenant_created', table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_tenant_id'), table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index(op.f('ix_api_credentials_tenant_id'), table_name='api_credentials')
    op.drop_table('api_credentials')
    op.drop_index(op.f('ix_access_credentials_tenant_id'), table_name='access_credentials')
    op.drop_table('access_credentials')
    op.drop_table('users')
    op.drop_table('tenants')
