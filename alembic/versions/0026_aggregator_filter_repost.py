"""aggregator: filter threshold + repost queue flags

Revision ID: 0026
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels", sa.Column("aggregate_min_score", sa.Integer(), nullable=True))
    op.add_column("posts", sa.Column("repost_pending", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("posts", sa.Column("repost_attempts", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("posts", "repost_attempts")
    op.drop_column("posts", "repost_pending")
    op.drop_column("target_channels", "aggregate_min_score")