"""target_channels: per-channel classify model/providers (маршрутизация из sources.yaml)

Revision ID: 0037
Revises: 0036
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("target_channels", sa.Column("classify_model", sa.String(255), nullable=True))
    op.add_column("target_channels", sa.Column("classify_providers", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("target_channels", "classify_providers")
    op.drop_column("target_channels", "classify_model")