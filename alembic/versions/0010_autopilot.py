"""autopilot and double check fields

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-05
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "target_channels",
        sa.Column("autopilot", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "target_channels",
        sa.Column("autopilot_min_score", sa.Integer(), nullable=True),
    )
    op.add_column(
        "target_channels",
        sa.Column("review_if_uncertain", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "target_channels",
        sa.Column("double_check", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "posts",
        sa.Column("autopilot", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "posts",
        sa.Column("double_check_note", sa.Text(), nullable=True),
    )
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE post_status ADD VALUE IF NOT EXISTS 'DOUBLE_CHECK_REVIEW'")


def downgrade() -> None:
    op.drop_column("posts", "double_check_note")
    op.drop_column("posts", "autopilot")
    op.drop_column("target_channels", "double_check")
    op.drop_column("target_channels", "review_if_uncertain")
    op.drop_column("target_channels", "autopilot_min_score")
    op.drop_column("target_channels", "autopilot")