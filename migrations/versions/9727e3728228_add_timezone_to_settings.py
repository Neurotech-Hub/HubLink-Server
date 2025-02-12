"""add timezone to settings

Revision ID: 9727e3728228
Revises: 1bba7015a915
Create Date: 2025-02-04 05:21:37.852591

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9727e3728228'
down_revision = '1bba7015a915'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('setting', schema=None) as batch_op:
        batch_op.add_column(sa.Column('timezone', sa.String(length=50), server_default='America/Chicago', nullable=False))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('setting', schema=None) as batch_op:
        batch_op.drop_column('timezone')

    # ### end Alembic commands ###
