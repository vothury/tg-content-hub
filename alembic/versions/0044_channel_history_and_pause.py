"""channels: history v2 knobs + owner pause columns.

Revision ID: 0044
Revises: 0043
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels",
                  sa.Column("read_history", sa.Boolean(),
                            nullable=False, server_default=sa.true()))
    op.add_column("target_channels",
                  sa.Column("history_max_posts", sa.Integer(), nullable=True))
    op.add_column("target_channels",
                  sa.Column("fresh_window_min", sa.Integer(), nullable=True))
    op.add_column("target_channels",
                  sa.Column("paused", sa.Boolean(),
                            nullable=False, server_default=sa.false()))
    op.add_column("sources",
                  sa.Column("paused", sa.Boolean(),
                            nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("sources", "paused")
    op.drop_column("target_channels", "paused")
    op.drop_column("target_channels", "fresh_window_min")
    op.drop_column("target_channels", "history_max_posts")
    op.drop_column("target_channels", "read_history")