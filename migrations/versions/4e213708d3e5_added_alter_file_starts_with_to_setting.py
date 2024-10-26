"""Added alter_file_starts_with to Setting

Revision ID: 4e213708d3e5
Revises: 775aee24ae90
Create Date: 2024-10-24 07:26:13.589677

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4e213708d3e5'
down_revision = '775aee24ae90'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('setting', schema=None) as batch_op:
        batch_op.add_column(sa.Column('alert_file_starts_with', sa.String(length=100), nullable=True))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('setting', schema=None) as batch_op:
        batch_op.drop_column('alert_file_starts_with')

    # ### end Alembic commands ###