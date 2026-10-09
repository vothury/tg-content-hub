"""sources: drop legacy freshness columns fallback_count/fallback_max_age_hours.

История v2 (read_history/history_max_posts/fresh_window_min на канале) заменила их;
reader и sync больше не читают эти колонки.

Revision ID: 0046
Revises: 0045
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("sources", "fallback_max_age_hours")
    op.drop_column("sources", "fallback_count")


def downgrade() -> None:
    op.add_column("sources", sa.Column("fallback_count", sa.Integer(), nullable=True))
    op.add_column("sources", sa.Column("fallback_max_age_hours", sa.Integer(), nullable=True))