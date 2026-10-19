"""editorial_articles.review: досье статьи для карточки ревью.

Рубрика, новизна, опора гипотезы, богатство материалов, сводка отчёта редактора —
вычисляются кодом в момент написания; страница ревью только читает.

Revision ID: 0048
Revises: 0047
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("editorial_articles",
                  sa.Column("review", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("editorial_articles", "review")