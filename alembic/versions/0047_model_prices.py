"""model_prices: ручной прайс-бук фактических цен пар модель+провайдер.

Пины провайдеров могут тарифицироваться иначе каталога слага (расхождение до 1.67x
измерено контрольной пробой); учёт стоимости и гарды бюджетов берут цену отсюда,
каталог остаётся запасным слоем.

Revision ID: 0047
Revises: 0046
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_price_book",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_slug", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=True),
        sa.Column("price_in_per_token", sa.Float(), nullable=False),
        sa.Column("price_out_per_token", sa.Float(), nullable=False),
        sa.Column("cache_read_per_token", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False,
                  server_default="manual"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("model_slug", "provider", name="uq_model_price_book_pair"),
    )


def downgrade() -> None:
    op.drop_table("model_price_book")