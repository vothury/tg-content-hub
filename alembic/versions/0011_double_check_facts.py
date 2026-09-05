"""double check online + fact strictness per channel

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels", sa.Column("double_check_online", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("target_channels", sa.Column("double_check_fact_strictness", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("target_channels", "double_check_fact_strictness")
    op.drop_column("target_channels", "double_check_online")