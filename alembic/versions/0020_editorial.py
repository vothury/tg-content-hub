"""editorial: virtual newsroom schema (web sources, headlines, topics, materials, articles)

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("editorial_only", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("target_channels", sa.Column("editorial", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "editorial_web_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False, unique=True),
        sa.Column("rewrite_source", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("list_selector", sa.Text(), nullable=True),
        sa.Column("title_selector", sa.Text(), nullable=True),
        sa.Column("link_selector", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "editorial_headlines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_kind", sa.String(8), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=True),
        sa.Column("url", sa.Text(), nullable=True, unique=True),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_hash", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("consumed_by_chief_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_editorial_headlines_title_hash", "editorial_headlines", ["title_hash"])
    op.create_table(
        "editorial_topics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.Enum("hypothesis", "rewrite", name="topic_kind"), nullable=False),
        sa.Column("theme", sa.Text(), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=True),
        sa.Column("why_interesting", sa.Text(), nullable=True),
        sa.Column("materials", JSONB(), nullable=True),
        sa.Column("confirm_signals", sa.Text(), nullable=True),
        sa.Column("refute_signals", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(16), nullable=True),
        sa.Column("status", sa.Enum("in_work", "ready", "published", "dropped", name="topic_status"),
                  nullable=False, server_default="in_work"),
        sa.Column("verdict", sa.String(16), nullable=True),
        sa.Column("verdict_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_editorial_topics_status", "editorial_topics", ["status"])
    op.create_table(
        "editorial_materials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("editorial_topics.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_editorial_materials_topic", "editorial_materials", ["topic_id"])
    op.create_table(
        "editorial_articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("editorial_topics.id"), nullable=False, unique=True),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Enum("draft", "review", "approved", "published", "rejected", name="article_status"),
                  nullable=False, server_default="draft"),
        sa.Column("publish_job_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_editorial_articles_status", "editorial_articles", ["status"])


def downgrade() -> None:
    op.drop_table("editorial_articles")
    op.drop_table("editorial_materials")
    op.drop_table("editorial_topics")
    op.drop_table("editorial_headlines")
    op.drop_table("editorial_web_sources")
    op.execute("DROP TYPE IF EXISTS topic_kind")
    op.execute("DROP TYPE IF EXISTS topic_status")
    op.execute("DROP TYPE IF EXISTS article_status")
    op.drop_column("target_channels", "editorial")
    op.drop_column("sources", "editorial_only")