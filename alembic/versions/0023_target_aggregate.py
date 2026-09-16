"""target_channels: no_review + aggregate_mode for technical aggregator channels

Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels", sa.Column("no_review", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("target_channels", sa.Column("aggregate_mode", sa.String(16), nullable=False, server_default="credit"))


def downgrade() -> None:
    op.drop_column("target_channels", "aggregate_mode")
    op.drop_column("target_channels", "no_review")