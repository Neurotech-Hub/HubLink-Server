"""group_by integer to Plot

Revision ID: c9d57bdd6e05
Revises: edab4d3400e1
Create Date: 2025-01-23 10:43:17.510861

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9d57bdd6e05'
down_revision = 'edab4d3400e1'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('plot', schema=None) as batch_op:
        batch_op.add_column(sa.Column('group_by', sa.Integer(), nullable=True))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('plot', schema=None) as batch_op:
        batch_op.drop_column('group_by')

    # ### end Alembic commands ###
