"""posts: published_text + published_links

Revision ID: 0031
Revises: 0030
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("published_text", sa.Text(), nullable=True))
    op.add_column("posts", sa.Column("published_links", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "published_links")
    op.drop_column("posts", "published_text")