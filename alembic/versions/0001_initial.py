"""initial schema

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

POST_STATUSES = (
    "NEW", "DEDUPLICATED", "PREFILTERED", "UNSUATABLE", "LLM_CLASSIFYING",
    "CANDIDATE", "REWRITING", "AWAITING_REVIEW", "NEEDS_MEDIA_REVIEW",
    "NEEDS_MANUAL_REVIEW", "REVISION", "MANUAL_EDITING", "APPROVED",
    "SCHEDULED", "PUBLISHING", "PUBLISHED", "REJECTED", "FAILED", "ARCHIVED",
)

ENUMS = (
    "source_kind", "post_status", "draft_origin", "media_type", "llm_stage",
    "llm_call_status", "event_actor", "publish_mode", "publish_job_state",
)


def _timestamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Enum("external", "test", name="source_kind"), nullable=False, server_default="external"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("poll_interval_sec", sa.Integer(), nullable=True),
        sa.Column("backfill_limit", sa.Integer(), nullable=True),
        sa.Column("last_read_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sources_username", "sources", ["username"])
    op.create_index("ix_sources_telegram_id", "sources", ["telegram_id"])

    op.create_table(
        "style_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("rewrite_prompt", sa.Text(), nullable=True),
        sa.Column("preserve_source_tone", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("examples", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_style_profiles_name"),
    )

    op.create_table(
        "target_channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("style_profile_id", sa.Integer(), sa.ForeignKey("style_profiles.id"), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("min_interval_min", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("quiet_hours", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_admin_check_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_target_channels_telegram_id", "target_channels", ["telegram_id"])

    op.create_table(
        "posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("post_url", sa.Text(), nullable=True),
        sa.Column("original_text", sa.Text(), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("text_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.Enum(*POST_STATUSES, name="post_status"), nullable=False, server_default="NEW"),
        sa.Column("needs_media_review", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("verdict_reason", sa.Text(), nullable=True),
        sa.Column("risks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column("draft_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("style_profile_id", sa.Integer(), sa.ForeignKey("style_profiles.id"), nullable=True),
        sa.Column("target_channel_id", sa.Integer(), sa.ForeignKey("target_channels.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "source_message_id", name="uq_posts_source_message"),
    )
    op.create_index("ix_posts_source_id", "posts", ["source_id"])
    op.create_index("ix_posts_text_hash", "posts", ["text_hash"])
    op.create_index("ix_posts_status", "posts", ["status"])

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("stage", sa.Enum("prefilter", "classify", "rewrite", "revision", name="llm_stage"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="openrouter"),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Enum("ok", "error", "parse_error", name="llm_call_status"), nullable=False, server_default="ok"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 8), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_calls_post_id", "llm_calls", ["post_id"])
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])

    op.create_table(
        "post_draft_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("origin", sa.Enum("llm_rewrite", "llm_revision", "manual", name="draft_origin"), nullable=False),
        sa.Column("llm_call_id", sa.Integer(), sa.ForeignKey("llm_calls.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id", "version", name="uq_draft_versions_post_version"),
    )
    op.create_index("ix_post_draft_versions_post_id", "post_draft_versions", ["post_id"])

    op.create_table(
        "media_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("media_type", sa.Enum("photo", "video", name="media_type"), nullable=False),
        sa.Column("group_id", sa.String(length=64), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("src_file_id", sa.String(length=255), nullable=True),
        sa.Column("src_file_unique_id", sa.String(length=255), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("mime", sa.String(length=64), nullable=True),
        sa.Column("downloaded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("download_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_items_post_id", "media_items", ["post_id"])

    op.create_table(
        "post_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor", sa.Enum("system", "owner", "llm", name="event_actor"), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_post_events_post_id", "post_events", ["post_id"])
    op.create_index("ix_post_events_created_at", "post_events", ["created_at"])

    op.create_table(
        "publish_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=False),
        sa.Column("target_channel_id", sa.Integer(), sa.ForeignKey("target_channels.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.Enum("now", "queue", "schedule", name="publish_mode"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.Enum("queued", "scheduled", "in_progress", "done", "failed", name="publish_job_state"), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("published_message_id", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_publish_jobs_idempotency_key"),
    )
    op.create_index("ix_publish_jobs_post_id", "publish_jobs", ["post_id"])
    op.create_index("ix_publish_jobs_target_channel_id", "publish_jobs", ["target_channel_id"])
    op.create_index("ix_publish_jobs_scheduled_at", "publish_jobs", ["scheduled_at"])

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    for table in (
        "app_settings", "publish_jobs", "post_events", "media_items",
        "post_draft_versions", "llm_calls", "posts", "target_channels",
        "style_profiles", "sources",
    ):
        op.drop_table(table)
    for enum_name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")