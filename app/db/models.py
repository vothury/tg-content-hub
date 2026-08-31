"""Модели данных. Источник истины — PostgreSQL; Redis только транспорт/локи."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import (
    DraftOrigin,
    EventActor,
    LLMCallStatus,
    LLMStage,
    MediaType,
    PostStatus,
    PublishJobState,
    PublishMode,
    SourceKind,
)


def _enum(enum_cls: type, name: str) -> SAEnum:
    """Native postgres-enum, значения берутся из .value элементов."""
    return SAEnum(enum_cls, name=name, values_callable=lambda c: [m.value for m in c])


class Source(Base):
    """Источник контента: чужой публичный канал или тестовый канал-лаборатория."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"), default=SourceKind.EXTERNAL)
    title: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    poll_interval_sec: Mapped[int | None] = mapped_column(Integer)
    backfill_limit: Mapped[int | None] = mapped_column(Integer)
    last_read_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Переопределения фильтров/чёрных списков на уровне источника
    filters: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    posts: Mapped[list[Post]] = relationship(back_populates="source")


class StyleProfile(Base):
    """Стилевой профиль целевого канала: промпты, примеры, режим сохранения тона."""

    __tablename__ = "style_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    rewrite_prompt: Mapped[str | None] = mapped_column(Text)
    # Режим «сохранить тон исходника»: простой рерайт без сильной смены стиля
    preserve_source_tone: Mapped[bool] = mapped_column(default=False)
    # Few-shot примеры, структура на стороне сервиса
    examples: Mapped[dict | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(default=1)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TargetChannel(Base):
    """Целевой канал пользователя. Публикация — только через Bot API после одобрения."""

    __tablename__ = "target_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    style_profile_id: Mapped[int | None] = mapped_column(ForeignKey("style_profiles.id"))
    enabled: Mapped[bool] = mapped_column(default=True)
    # Лимиты публикаций
    daily_limit: Mapped[int] = mapped_column(default=6)
    min_interval_min: Mapped[int] = mapped_column(default=60)
    # Например {"start": "23:00", "end": "08:00"}
    quiet_hours: Mapped[dict | None] = mapped_column(JSONB)
    last_admin_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Post(Base):
    """Найденный пост источника. Жёсткая дедупликация: (source_id, source_message_id)."""

    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("source_id", "source_message_id", name="uq_posts_source_message"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    post_url: Mapped[str | None] = mapped_column(Text)
    original_text: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    # Дополнительная дедупликация по содержимому
    text_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[PostStatus] = mapped_column(_enum(PostStatus, "post_status"), default=PostStatus.NEW, index=True)
    # Визуально-ориентированный пост: не отклонять, показать владельцу
    needs_media_review: Mapped[bool] = mapped_column(default=False)

    # Результат этапа оценки (LLM classify)
    score: Mapped[float | None] = mapped_column(Float)
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    risks: Mapped[dict | None] = mapped_column(JSONB)

    # Текущий черновик; история — в post_draft_versions
    draft_text: Mapped[str | None] = mapped_column(Text)
    draft_version: Mapped[int] = mapped_column(default=0)

    style_profile_id: Mapped[int | None] = mapped_column(ForeignKey("style_profiles.id"))
    # Выбирается владельцем при одобрении
    target_channel_id: Mapped[int | None] = mapped_column(ForeignKey("target_channels.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    source: Mapped[Source] = relationship(back_populates="posts")
    media_items: Mapped[list[MediaItem]] = relationship(back_populates="post", cascade="all, delete-orphan")
    draft_versions: Mapped[list[PostDraftVersion]] = relationship(back_populates="post", cascade="all, delete-orphan")
    events: Mapped[list[PostEvent]] = relationship(back_populates="post", cascade="all, delete-orphan")


class PostDraftVersion(Base):
    """История версий черновика: рерайт, правка ИИ, ручная правка."""

    __tablename__ = "post_draft_versions"
    __table_args__ = (UniqueConstraint("post_id", "version", name="uq_draft_versions_post_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    origin: Mapped[DraftOrigin] = mapped_column(_enum(DraftOrigin, "draft_origin"))
    llm_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_calls.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    post: Mapped[Post] = relationship(back_populates="draft_versions")


class MediaItem(Base):
    """Медиа поста. ВАЖНО: при публикации бот загружает файл заново из local_path,
    т.к. file_id пользовательской сессии несовместим с ботом."""

    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    media_type: Mapped[MediaType] = mapped_column(_enum(MediaType, "media_type"))
    # Альбом: общий group_id + позиция элемента
    group_id: Mapped[str | None] = mapped_column(String(64))
    position: Mapped[int | None] = mapped_column(Integer)
    src_file_id: Mapped[str | None] = mapped_column(String(255))
    src_file_unique_id: Mapped[str | None] = mapped_column(String(255))
    local_path: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    mime: Mapped[str | None] = mapped_column(String(64))
    downloaded: Mapped[bool] = mapped_column(default=False)
    download_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    post: Mapped[Post] = relationship(back_populates="media_items")


class LLMCall(Base):
    """Полный лог каждого вызова модели: промпт, ответ, токены, стоимость."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id", ondelete="SET NULL"), index=True)
    stage: Mapped[LLMStage] = mapped_column(_enum(LLMStage, "llm_stage"))
    provider: Mapped[str] = mapped_column(String(32), default="openrouter")
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    request: Mapped[dict | None] = mapped_column(JSONB)
    response: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[LLMCallStatus] = mapped_column(_enum(LLMCallStatus, "llm_call_status"), default=LLMCallStatus.OK)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PostEvent(Base):
    """Аудит всех действий и переходов статуса."""

    __tablename__ = "post_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    actor: Mapped[EventActor] = mapped_column(_enum(EventActor, "event_actor"))
    action: Mapped[str] = mapped_column(String(64))
    # Строки, а не enum: устойчиво к расширению набора статусов
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    post: Mapped[Post] = relationship(back_populates="events")


class PublishJob(Base):
    """Задача публикации. Идемпотентность через idempotency_key."""

    __tablename__ = "publish_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    target_channel_id: Mapped[int] = mapped_column(ForeignKey("target_channels.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    mode: Mapped[PublishMode] = mapped_column(_enum(PublishMode, "publish_mode"))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[PublishJobState] = mapped_column(
        _enum(PublishJobState, "publish_job_state"), default=PublishJobState.QUEUED
    )
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    last_error: Mapped[str | None] = mapped_column(Text)
    published_message_id: Mapped[int | None] = mapped_column(BigInteger)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AppSetting(Base):
    """Runtime-переопределения настроек из .env (модели, предохранители).
    Значение — любой JSON. Эффективное значение = переопределение или дефолт из env."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())