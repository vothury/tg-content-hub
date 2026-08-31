"""привязка источников к целевым каналам

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("target_channel_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_sources_target_channel", "sources", "target_channels",
                          ["target_channel_id"], ["id"])
    op.create_index("ix_sources_target_channel_id", "sources", ["target_channel_id"])


def downgrade() -> None:
    op.drop_index("ix_sources_target_channel_id", table_name="sources")
    op.drop_constraint("fk_sources_target_channel", "sources", type_="foreignkey")
    op.drop_column("sources", "target_channel_id")