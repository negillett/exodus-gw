"""Add tasks table

Revision ID: cd561983acb2
Revises: 0c60e1b25e03
Create Date: 2021-02-08 18:08:31.508678

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.types import Uuid

# revision identifiers, used by Alembic.
revision = "cd561983acb2"
down_revision = "0c60e1b25e03"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "tasks",
        sa.Column("id", Uuid(as_uuid=False), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("publish_id", Uuid(as_uuid=False), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("tasks")
    # ### end Alembic commands ###
