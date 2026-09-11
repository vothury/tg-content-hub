"""dedup: store evaluation metrics per post

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("dedup_info", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "dedup_info")