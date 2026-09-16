"""target_channels: autopilot_sig_guard

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels", sa.Column("autopilot_sig_guard", sa.Boolean(),
                                               nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("target_channels", "autopilot_sig_guard")