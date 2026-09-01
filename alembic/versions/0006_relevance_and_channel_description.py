"""релевантность источника и описание целевого канала

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("relevance", sa.Integer(), nullable=True))
    op.add_column("target_channels", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("target_channels", "description")
    op.drop_column("sources", "relevance")