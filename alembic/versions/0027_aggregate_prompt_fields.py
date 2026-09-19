"""target_channels: per-channel aggregate filter prompt fields

Revision ID: 0027
Revises: 0026
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels", sa.Column("aggregate_accept", sa.Text(), nullable=True))
    op.add_column("target_channels", sa.Column("aggregate_reject", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("target_channels", "aggregate_reject")
    op.drop_column("target_channels", "aggregate_accept")