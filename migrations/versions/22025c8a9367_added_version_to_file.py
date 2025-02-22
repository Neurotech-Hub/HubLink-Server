"""added version to File

Revision ID: 22025c8a9367
Revises: e18086ed7253
Create Date: 2024-11-19 13:24:52.432729

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '22025c8a9367'
down_revision = 'e18086ed7253'
branch_labels = None
depends_on = None


def upgrade():
    # Add the column as nullable first
    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.add_column(sa.Column('version', sa.Integer(), nullable=True))
    
    # Update existing rows to have version=1
    op.execute('UPDATE file SET version = 1 WHERE version IS NULL')
    
    # Now make the column non-nullable
    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.alter_column('version',
                            existing_type=sa.Integer(),
                            nullable=False)


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.drop_column('version')
    # ### end Alembic commands ###
