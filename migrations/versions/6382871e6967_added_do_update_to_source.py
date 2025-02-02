"""added do_update to source

Revision ID: 6382871e6967
Revises: 99667ceb688c
Create Date: 2025-01-26 11:04:03.760036

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6382871e6967'
down_revision = '99667ceb688c'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('source', schema=None) as batch_op:
        batch_op.add_column(sa.Column('do_update', sa.Boolean(), server_default='0', nullable=False))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('source', schema=None) as batch_op:
        batch_op.drop_column('do_update')

    # ### end Alembic commands ###
