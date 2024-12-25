"""added time range to layout

Revision ID: 103e046d97fe
Revises: bba60573ae5c
Create Date: 2024-12-25 06:57:16.423918

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '103e046d97fe'
down_revision = 'bba60573ae5c'
branch_labels = None
depends_on = None


def upgrade():
    # First add the column as nullable
    with op.batch_alter_table('layout', schema=None) as batch_op:
        batch_op.add_column(sa.Column('time_range', sa.String(length=20), nullable=True))

    # Update existing rows to have 'all' as the time_range
    op.execute("UPDATE layout SET time_range = 'all' WHERE time_range IS NULL")

    # Now make the column non-nullable
    with op.batch_alter_table('layout', schema=None) as batch_op:
        batch_op.alter_column('time_range',
                            existing_type=sa.String(length=20),
                            nullable=False)


def downgrade():
    with op.batch_alter_table('layout', schema=None) as batch_op:
        batch_op.drop_column('time_range')
