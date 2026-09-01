"""rewrite_enabled у целевого канала и происхождение черновика 'original'

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "target_channels",
        sa.Column("rewrite_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE draft_origin ADD VALUE IF NOT EXISTS 'original'")


def downgrade() -> None:
    op.drop_column("target_channels", "rewrite_enabled")
    # значение 'original' из enum не удаляем — это безопасно оставить