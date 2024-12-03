"""changing plot.info to plot.data

Revision ID: 20f8cb4ccb98
Revises: c63b2f2f6c6b
Create Date: 2024-12-03 07:26:48.848933

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20f8cb4ccb98'
down_revision = 'c63b2f2f6c6b'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('plot', schema=None) as batch_op:
        batch_op.add_column(sa.Column('data', sa.Text(), nullable=True))
        batch_op.drop_column('info')

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('plot', schema=None) as batch_op:
        batch_op.add_column(sa.Column('info', sa.TEXT(), nullable=True))
        batch_op.drop_column('data')

    # ### end Alembic commands ###
