"""target_channels: per-channel llm_instructions

Revision ID: 0029
Revises: 0028
"""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels", sa.Column("llm_instructions", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("target_channels", "llm_instructions")