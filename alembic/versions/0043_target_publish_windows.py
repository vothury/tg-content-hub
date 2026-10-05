"""target_channels: publish_windows - разрешённые окна публикации канала.

Revision ID: 0043
Revises: 0042
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels",
                  sa.Column("publish_windows", sa.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("target_channels", "publish_windows")