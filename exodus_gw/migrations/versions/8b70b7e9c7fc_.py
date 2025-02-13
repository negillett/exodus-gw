"""add deadline column to tasks

Revision ID: 8b70b7e9c7fc
Revises: 48cfe99f5c21
Create Date: 2022-07-28 10:16:38.524859

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "8b70b7e9c7fc"
down_revision = "48cfe99f5c21"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        "tasks",
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
    )
    # ### end Alembic commands ###


def downgrade():
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("deadline")
