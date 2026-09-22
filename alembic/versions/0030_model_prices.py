"""model price watch: history + alerts

Revision ID: 0030
Revises: 0029
"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("prompt_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("completion_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("request_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_model_prices_model", "model_prices", ["model"])
    op.create_table(
        "model_price_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("old_prompt", sa.Float(), nullable=True),
        sa.Column("new_prompt", sa.Float(), nullable=True),
        sa.Column("old_completion", sa.Float(), nullable=True),
        sa.Column("new_completion", sa.Float(), nullable=True),
        sa.Column("change_pct", sa.Float(), nullable=True),
        sa.Column("direction", sa.String(8), nullable=True),
        sa.Column("acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_model_price_alerts_open", "model_price_alerts", ["acknowledged"])


def downgrade() -> None:
    op.drop_table("model_price_alerts")
    op.drop_table("model_prices")