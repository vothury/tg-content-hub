"""posts: уникальность (source, message) с учётом целевого канала

Курирование создаёт клон поста на каждый целевой канал — пара
(source_id, source_message_id) у клонов одинакова.

Revision ID: 0036
Revises: 0035
"""
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_posts_source_message", "posts", type_="unique")
    op.create_unique_constraint(
        "uq_posts_source_message_target",
        "posts",
        ["source_id", "source_message_id", "target_channel_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_posts_source_message_target", "posts", type_="unique")
    op.create_unique_constraint(
        "uq_posts_source_message", "posts", ["source_id", "source_message_id"]
    )