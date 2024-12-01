"""switching plots to use json config field

Revision ID: 79ac6046ed5b
Revises: 0082d1e7bafe
Create Date: 2024-12-01 08:06:16.543303

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '79ac6046ed5b'
down_revision = '0082d1e7bafe'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('plot', schema=None) as batch_op:
        batch_op.add_column(sa.Column('config', sa.String(length=500), nullable=False))
        batch_op.drop_column('x_column')
        batch_op.drop_column('y_column')

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('plot', schema=None) as batch_op:
        batch_op.add_column(sa.Column('y_column', sa.VARCHAR(length=100), nullable=False))
        batch_op.add_column(sa.Column('x_column', sa.VARCHAR(length=100), nullable=False))
        batch_op.drop_column('config')

    # ### end Alembic commands ###
