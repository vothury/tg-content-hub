"""target_channels: dedup_enabled — опциональный семантический дедуп
(off => дубли не подавляются, canonical не строится)

Revision ID: 0040
Revises: 0039
"""
import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels",
                  sa.Column("dedup_enabled", sa.Boolean(),
                            nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("target_channels", "dedup_enabled")