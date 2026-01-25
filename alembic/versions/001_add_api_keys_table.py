"""Add api_keys table for API key management

Revision ID: add_api_keys_table
Revises: <your_previous_revision_id>
Create Date: 2026-01-24 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime


# revision identifiers, used by Alembic.
revision = 'add_api_keys_table'
down_revision = '1f57fc4f4605'  # ✅ Links to initial_schema migration
depends_on = None


def upgrade():
    """
    Create api_keys table with proper indexes
    """
    # Create api_keys table
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('key_prefix', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False, server_default='Default API Key'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        
        # Primary key
        sa.PrimaryKeyConstraint('id'),
        
        # Foreign key to users table
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        
        # Unique constraint on key_hash (each key hash must be unique)
        sa.UniqueConstraint('key_hash', name='uq_api_key_hash')
    )
    
    # Create indexes for performance
    op.create_index('idx_api_key_hash', 'api_keys', ['key_hash'])
    op.create_index('idx_api_key_user', 'api_keys', ['user_id'])
    op.create_index('idx_api_key_active', 'api_keys', ['is_active'])
    op.create_index('ix_api_keys_id', 'api_keys', ['id'])
    
    print("✅ Created api_keys table with indexes")


def downgrade():
    """
    Drop api_keys table and indexes
    """
    # Drop indexes first
    op.drop_index('idx_api_key_active', table_name='api_keys')
    op.drop_index('idx_api_key_user', table_name='api_keys')
    op.drop_index('idx_api_key_hash', table_name='api_keys')
    op.drop_index('ix_api_keys_id', table_name='api_keys')
    
    # Drop table
    op.drop_table('api_keys')
    
    print("✅ Dropped api_keys table and indexes")