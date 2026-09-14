"""posts: recap_ids for duplicate-of-published annotation

Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("recap_ids", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "recap_ids")