"""headlines: привязка к теме главреда и статус жизненного цикла.

Таблицы редакции живут с префиксом editorial_: editorial_headlines, editorial_topics.
status: new -> picked (взята в тему) / used (вошла в статью) / dropped (отклонена);
topic_id: FK на editorial_topics, при удалении темы обнуляется (SET NULL).

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
    op.add_column("editorial_headlines",
                  sa.Column("topic_id", sa.Integer(), nullable=True))
    op.add_column("editorial_headlines",
                  sa.Column("status", sa.String(length=16),
                            server_default="new", nullable=False))
    op.create_foreign_key("fk_editorial_headlines_topic_id",
                          "editorial_headlines", "editorial_topics",
                          ["topic_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_editorial_headlines_topic_id",
                    "editorial_headlines", ["topic_id"])
    op.create_index("ix_editorial_headlines_status",
                    "editorial_headlines", ["status"])


def downgrade() -> None:
    op.drop_index("ix_editorial_headlines_status", table_name="editorial_headlines")
    op.drop_index("ix_editorial_headlines_topic_id", table_name="editorial_headlines")
    op.drop_constraint("fk_editorial_headlines_topic_id",
                       "editorial_headlines", type_="foreignkey")
    op.drop_column("editorial_headlines", "status")
    op.drop_column("editorial_headlines", "topic_id")