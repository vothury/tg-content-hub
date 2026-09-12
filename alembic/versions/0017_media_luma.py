"""media: luma signature for dedup guard

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media_items", sa.Column("luma_mean", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_items", "luma_mean")