"""настройки свежести постов на источник

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("fresh_window_min", sa.Integer(), nullable=True))
    op.add_column("sources", sa.Column("fallback_count", sa.Integer(), nullable=True))
    op.add_column("sources", sa.Column("fallback_max_age_hours", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "fallback_max_age_hours")
    op.drop_column("sources", "fallback_count")
    op.drop_column("sources", "fresh_window_min")