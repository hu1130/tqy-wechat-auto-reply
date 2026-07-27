"""add agent activations

Revision ID: 20260723_0003
Revises: 20260723_0002
Create Date: 2026-07-23 10:42:56.242492
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa



revision: str = '20260723_0003'
down_revision: Union[str, Sequence[str], None] = '20260723_0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('agent_activations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('agent_id', sa.String(length=36), nullable=False),
    sa.Column('channel_account_id', sa.String(length=36), nullable=False),
    sa.Column('code_hash', sa.String(length=128), nullable=False),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('expires_in_seconds', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by_user_id', sa.String(length=36), nullable=False),
    sa.Column('consumed_device_hash', sa.String(length=128), nullable=True),
    sa.Column('consumed_device_name', sa.String(length=200), nullable=True),
    sa.Column('consumed_agent_version', sa.String(length=100), nullable=True),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('expires_in_seconds BETWEEN 300 AND 86400', name='ck_agent_activation_expiry_seconds'),
    sa.ForeignKeyConstraint(['agent_id'], ['agent_instances.id'], ),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], ),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code_hash', name='uq_agent_activation_code_hash'),
    sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_agent_activation_tenant_idempotency')
    )
    op.create_index('ix_agent_activation_consume', 'agent_activations', ['code_hash', 'consumed_at', 'expires_at'], unique=False)
    op.create_index(op.f('ix_agent_activations_agent_id'), 'agent_activations', ['agent_id'], unique=False)
    op.create_index(op.f('ix_agent_activations_channel_account_id'), 'agent_activations', ['channel_account_id'], unique=False)
    op.create_index(op.f('ix_agent_activations_tenant_id'), 'agent_activations', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_agent_activations_tenant_id'), table_name='agent_activations')
    op.drop_index(op.f('ix_agent_activations_channel_account_id'), table_name='agent_activations')
    op.drop_index(op.f('ix_agent_activations_agent_id'), table_name='agent_activations')
    op.drop_index('ix_agent_activation_consume', table_name='agent_activations')
    op.drop_table('agent_activations')
