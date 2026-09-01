"""Централизованная конфигурация. Все секреты — только из окружения."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Среда
    environment: str = "dev"
    log_level: str = "INFO"

    # Хранилища
    database_url: str = "postgresql+asyncpg://content_hub:content_hub@postgres:5432/content_hub"
    redis_url: str = "redis://redis:6379/0"
    media_dir: str = "media"

    # Telegram: чтение источников (отдельный аккаунт)
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    reader_session_path: str = "sessions/reader"
    reader_poll_interval_sec: int = 30
    reader_backfill_limit: int = 20

    # Telegram: бот (публикация и ревью)
    bot_token: str = ""
    allowed_owner_ids: list[int] = Field(default_factory=list)

    # LLM / OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    prefilter_model: str = "deepseek/deepseek-v4-flash-0731"
    classify_model: str = "deepseek/deepseek-v4-flash-0731"
    rewrite_model: str = "openai/gpt-5.6-luna"
    revision_model: str = ""  # пусто = модель рерайта

    # Предохранители
    max_llm_budget_usd_per_day: float = 1.0
    max_candidates_per_day: int = 30

    # Админка (этап 6)
    admin_password: str = ""

    # Читатель: интервал опроса источника по умолчанию (если не задан у источника)
    reader_default_source_interval_sec: int = 120
    # Предохранитель: медиа крупнее не скачиваются (Этап 1)
    max_media_download_mb: int = 25

    # Предфильтр (Этап 2)
    prefilter_min_text_len: int = 200
    prefilter_blacklist_words: list[str] = Field(default_factory=list)

    # LLM (Этап 3)
    openrouter_request_timeout_sec: int = 90
    llm_classify_max_tokens: int = 800
    llm_rewrite_max_tokens: int = 1500
    # Как часто пайплайн пересматривает «застрявшие» посты при пустой очереди
    pipeline_rescan_interval_sec: int = 60

    # Предпочтения провайдеров OpenRouter (JSON; пусто = авто-маршрутизация)
    # Пример: {"order": ["OpenAI"], "allow_fallbacks": false, "quantizations": ["fp8"]}
    classify_providers: str = ""
    rewrite_providers: str = ""
    revision_providers: str = ""

    # Ревью (Этап 4): период поиска постов без отправленной карточки
    review_poll_interval_sec: int = 30

    # Политика свежести постов (Этап 4+)
    reader_fresh_window_min: int = 60          # окно свежести, минуты
    reader_fallback_count: int = 2             # если свежих нет: взять последних
    reader_fallback_max_age_hours: int = 48    # но не старше этого возраста

    # Публикация (Этап 5)
    owner_timezone: str = "UTC"           # часовой пояс владельца (например, Europe/Moscow)
    scheduler_poll_interval_sec: int = 15

    # Веб-админка (Этап 6)
    admin_password: str = ""
    secret_key: str = ""

    @property
    def effective_revision_model(self) -> str:
        return self.revision_model or self.rewrite_model


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()