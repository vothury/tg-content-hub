"""headlines: привязка к теме главреда и статус жизненного цикла.

status: new -> picked (взята в тему) / used (вошла в статью) / dropped (отклонена);
topic_id: FK на topics, при удалении темы обнуляется (SET NULL), заголовок живёт дальше.
Индексы: статус (фильтры ленты/прайт) и topic_id (обратные связи тема -> заголовки).

Revision ID: 0041
Revises: 0040
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("headlines", sa.Column("topic_id", sa.Integer(), nullable=True))
    op.add_column("headlines", sa.Column("status", sa.String(length=16),
                                          server_default="new", nullable=False))
    op.create_foreign_key("fk_headlines_topic_id", "headlines", "topics",
                          ["topic_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_headlines_topic_id", "headlines", ["topic_id"])
    op.create_index("ix_headlines_status", "headlines", ["status"])


def downgrade() -> None:
    op.drop_index("ix_headlines_status", table_name="headlines")
    op.drop_index("ix_headlines_topic_id", table_name="headlines")
    op.drop_constraint("fk_headlines_topic_id", "headlines", type_="foreignkey")
    op.drop_column("headlines", "status")
    op.drop_column("headlines", "topic_id")