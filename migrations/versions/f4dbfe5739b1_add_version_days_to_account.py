"""add version days to account

Revision ID: f4dbfe5739b1
Revises: fb0330cdce7a
Create Date: 2025-02-09 09:38:45.764004

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4dbfe5739b1'
down_revision = 'fb0330cdce7a'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan_version_days', sa.Integer(), server_default='7', nullable=False))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.drop_column('plan_version_days')

    # ### end Alembic commands ###
