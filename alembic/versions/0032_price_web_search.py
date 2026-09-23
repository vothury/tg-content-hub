"""model_prices: web_search price + history threshold setting

Revision ID: 0032
Revises: 0031
"""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_prices", sa.Column("web_search_usd", sa.Float(),
                                            nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("model_prices", "web_search_usd")