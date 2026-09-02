"""дата публикации поста в канале-источнике

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "source_published_at")