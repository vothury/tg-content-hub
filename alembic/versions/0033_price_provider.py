"""model prices: provider dimension

Revision ID: 0033
Revises: 0032
"""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_prices", sa.Column("provider", sa.String(160),
                                            nullable=False, server_default=""))
    op.add_column("model_price_alerts", sa.Column("provider", sa.String(160),
                                                  nullable=False, server_default=""))
    op.create_index("ix_model_prices_model_provider", "model_prices", ["model", "provider"])


def downgrade() -> None:
    op.drop_index("ix_model_prices_model_provider", table_name="model_prices")
    op.drop_column("model_price_alerts", "provider")
    op.drop_column("model_prices", "provider")