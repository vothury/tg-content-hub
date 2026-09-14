"""target_channels: dup_recap_enabled (per-channel recap for duplicates)

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels", sa.Column("dup_recap_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("target_channels", "dup_recap_enabled")