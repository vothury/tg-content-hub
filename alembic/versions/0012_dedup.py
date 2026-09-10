"""dedup: canonical text + media phash

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("canonical_text", sa.Text(), nullable=True))
    op.add_column("media_items", sa.Column("phash", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_items", "phash")
    op.drop_column("posts", "canonical_text")