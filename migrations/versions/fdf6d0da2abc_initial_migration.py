"""initial migration

Revision ID: fdf6d0da2abc
Revises: 
Create Date: 2025-03-15 10:06:53.390109

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'fdf6d0da2abc'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('account',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), server_default='', nullable=False),
    sa.Column('url', sa.String(length=200), server_default='', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('plan_uploads_mo', sa.Integer(), server_default='500', nullable=False),
    sa.Column('plan_storage_gb', sa.Integer(), server_default='10', nullable=False),
    sa.Column('plan_versioned_backups', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('plan_version_days', sa.Integer(), server_default='7', nullable=False),
    sa.Column('plan_start_date', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('storage_current_bytes', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('storage_versioned_bytes', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('count_gateway_pings', sa.Integer(), server_default='0', nullable=False),
    sa.Column('count_uploaded_files', sa.Integer(), server_default='0', nullable=False),
    sa.Column('count_uploaded_files_mo', sa.Integer(), server_default='0', nullable=False),
    sa.Column('count_file_downloads', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_admin', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('password_hash', sa.String(length=256), nullable=True),
    sa.Column('use_password', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('url')
    )
    op.create_table('admin',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('last_daily_cron', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('file',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=200), server_default='', nullable=False),
    sa.Column('url', sa.String(length=500), server_default='', nullable=False),
    sa.Column('size', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('last_modified', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('last_checked', sa.DateTime(timezone=True), nullable=True),
    sa.Column('archived', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['account.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('gateway',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('ip_address', sa.String(length=45), server_default='', nullable=False),
    sa.Column('name', sa.String(length=100), server_default='', nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['account.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('layout',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), server_default='', nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('show_nav', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('time_range', sa.String(length=20), server_default='all', nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['account.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('setting',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('aws_access_key_id', sa.String(length=200), server_default='', nullable=True),
    sa.Column('aws_secret_access_key', sa.String(length=200), server_default='', nullable=True),
    sa.Column('bucket_name', sa.String(length=200), server_default='', nullable=True),
    sa.Column('max_file_size', sa.BigInteger(), server_default='1073741824', nullable=False),
    sa.Column('use_cloud', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('device_name_includes', sa.String(length=100), server_default='HUBLINK', nullable=True),
    sa.Column('alert_email', sa.String(length=100), server_default='', nullable=True),
    sa.Column('gateway_manages_memory', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('timezone', sa.String(length=50), server_default='America/Chicago', nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['account.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('node',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gateway_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['gateway_id'], ['gateway.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('source',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), server_default='', nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('directory_filter', sa.String(length=200), server_default='*', nullable=False),
    sa.Column('include_subdirs', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('include_columns', sa.String(length=500), server_default='', nullable=False),
    sa.Column('data_points', sa.Integer(), server_default='0', nullable=False),
    sa.Column('tail_only', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('datetime_column', sa.String(length=100), server_default='', nullable=False),
    sa.Column('last_updated', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.String(length=500), server_default='', nullable=True),
    sa.Column('file_id', sa.Integer(), nullable=True),
    sa.Column('state', sa.String(length=50), server_default='created', nullable=False),
    sa.Column('max_path_level', sa.Integer(), server_default='0', nullable=False),
    sa.Column('do_update', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['account.id'], ),
    sa.ForeignKeyConstraint(['file_id'], ['file.id'], name='fk_source_file'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('plot',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), server_default='', nullable=False),
    sa.Column('type', sa.String(length=50), server_default='timeline', nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('group_by', sa.Integer(), nullable=True),
    sa.Column('advanced', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['source.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('plot')
    op.drop_table('source')
    op.drop_table('node')
    op.drop_table('setting')
    op.drop_table('layout')
    op.drop_table('gateway')
    op.drop_table('file')
    op.drop_table('admin')
    op.drop_table('account')
    # ### end Alembic commands ###
