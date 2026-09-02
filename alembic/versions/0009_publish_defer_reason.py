"""причина отсрочки публикации

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("publish_jobs", sa.Column("defer_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("publish_jobs", "defer_reason")