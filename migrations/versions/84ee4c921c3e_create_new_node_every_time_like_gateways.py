"""create new node every time like gateways

Revision ID: 84ee4c921c3e
Revises: 9727e3728228
Create Date: 2025-02-04 09:57:17.082756

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '84ee4c921c3e'
down_revision = '9727e3728228'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('node', schema=None) as batch_op:
        # First drop the last_modified column since it's no longer needed
        batch_op.drop_column('last_modified')
        
        # Then recreate the table with the new column types and constraints
        batch_op.alter_column('uuid',
               existing_type=sa.VARCHAR(length=36),
               type_=sa.String(length=100),
               existing_nullable=False,
               existing_server_default=sa.text("('')"))
               
        # For SQLite, we need to drop and recreate the foreign key
        # Create new foreign key with CASCADE delete
        batch_op.create_foreign_key(
            'fk_node_gateway_id', 
            'gateway',
            ['gateway_id'], ['id'],
            ondelete='CASCADE'
        )

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('node', schema=None) as batch_op:
        # Add back the last_modified column
        batch_op.add_column(sa.Column('last_modified', sa.DATETIME(), 
                                    server_default=sa.text('(CURRENT_TIMESTAMP)'), 
                                    nullable=True))
        
        # Recreate the original foreign key without CASCADE
        batch_op.create_foreign_key(
            'fk_node_gateway_id',
            'gateway',
            ['gateway_id'], ['id']
        )
        
        # Revert the uuid column type
        batch_op.alter_column('uuid',
               existing_type=sa.String(length=100),
               type_=sa.VARCHAR(length=36),
               existing_nullable=False,
               existing_server_default=sa.text("('')"))

    # ### end Alembic commands ###
