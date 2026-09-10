"""Эффективные настройки: переопределения из app_settings поверх дефолтов из .env.

Админка (этап 6) будет писать сюда модели, провайдеров, предохранители и
параметры предфильтра без рестарта сервисов.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

log = logging.getLogger(__name__)


class Keys:
    PREFILTER_MODEL = "llm.prefilter_model"
    CLASSIFY_MODEL = "llm.classify_model"
    REWRITE_MODEL = "llm.rewrite_model"
    REVISION_MODEL = "llm.revision_model"
    CLASSIFY_PROVIDERS = "llm.classify_providers"
    REWRITE_PROVIDERS = "llm.rewrite_providers"
    REVISION_PROVIDERS = "llm.revision_providers"
    MAX_LLM_BUDGET_USD_PER_DAY = "limits.max_llm_budget_usd_per_day"
    MAX_CANDIDATES_PER_DAY = "limits.max_candidates_per_day"
    PREFILTER_MIN_TEXT_LEN = "prefilter.min_text_len"
    PREFILTER_BLACKLIST_WORDS = "prefilter.blacklist_words"
    MAX_MEDIA_DOWNLOAD_MB = "reader.max_media_download_mb"
    READER_DEFAULT_SOURCE_INTERVAL_SEC = "reader.default_source_interval_sec"
    AUTOPILOT_MIN_SCORE = "autopilot.min_score"
    DOUBLE_CHECK_MODEL = "llm.double_check_model"
    DOUBLE_CHECK_ONLINE_MODEL = "llm.double_check_online_model"
    CLASSIFY_VERBOSE = "llm.classify_verbose"
    PREFILTER_PROVIDERS = "llm.prefilter_providers"
    DOUBLE_CHECK_PROVIDERS = "llm.double_check_providers"
    DOUBLE_CHECK_ONLINE_PROVIDERS = "llm.double_check_online_providers"
    DEDUP_WINDOW_DAYS = "dedup.window_days"
    DEDUP_PHASH_MAX_DISTANCE = "dedup.phash_max_distance"
    DEDUP_CANONICAL_MIN_LEN = "dedup.canonical_min_len"
    DEDUP_CANONICAL_COSINE_MIN = "dedup.canonical_cosine_min"
    DEDUP_MAX_COMPARE = "dedup.max_compare"


_ENV_DEFAULTS: dict[str, Any] = {
    Keys.PREFILTER_MODEL: settings.prefilter_model,
    Keys.CLASSIFY_MODEL: settings.classify_model,
    Keys.REWRITE_MODEL: settings.rewrite_model,
    Keys.REVISION_MODEL: settings.effective_revision_model,
    Keys.CLASSIFY_PROVIDERS: settings.classify_providers,
    Keys.REWRITE_PROVIDERS: settings.rewrite_providers,
    Keys.REVISION_PROVIDERS: settings.revision_providers,
    Keys.MAX_LLM_BUDGET_USD_PER_DAY: settings.max_llm_budget_usd_per_day,
    Keys.MAX_CANDIDATES_PER_DAY: settings.max_candidates_per_day,
    Keys.PREFILTER_MIN_TEXT_LEN: settings.prefilter_min_text_len,
    Keys.PREFILTER_BLACKLIST_WORDS: settings.prefilter_blacklist_words,
    Keys.MAX_MEDIA_DOWNLOAD_MB: settings.max_media_download_mb,
    Keys.READER_DEFAULT_SOURCE_INTERVAL_SEC: settings.reader_default_source_interval_sec,
    Keys.AUTOPILOT_MIN_SCORE: settings.autopilot_min_score,
    Keys.DOUBLE_CHECK_MODEL: settings.double_check_model,
    Keys.DOUBLE_CHECK_ONLINE_MODEL: settings.double_check_online_model,
    Keys.CLASSIFY_VERBOSE: settings.classify_verbose,
    Keys.PREFILTER_PROVIDERS: settings.prefilter_providers,
    Keys.DOUBLE_CHECK_PROVIDERS: settings.double_check_providers,
    Keys.DOUBLE_CHECK_ONLINE_PROVIDERS: settings.double_check_online_providers,
    Keys.DEDUP_WINDOW_DAYS: settings.dedup_window_days,
    Keys.DEDUP_PHASH_MAX_DISTANCE: settings.dedup_phash_max_distance,
    Keys.DEDUP_CANONICAL_MIN_LEN: settings.dedup_canonical_min_len,
    Keys.DEDUP_CANONICAL_COSINE_MIN: settings.dedup_canonical_cosine_min,
    Keys.DEDUP_MAX_COMPARE: settings.dedup_max_compare,
}


async def get_setting(session: AsyncSession, key: str) -> Any:
    """Значение из БД-переопределения или дефолт из окружения."""
    from app.db.models import AppSetting  # локальный импорт против циклов

    row = await session.get(AppSetting, key)
    if row is not None and row.value is not None:
        return row.value
    return _ENV_DEFAULTS.get(key)


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    from app.db.models import AppSetting

    row = await session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key)
        session.add(row)
    row.value = value
    await session.commit()


async def get_providers(session: AsyncSession, key: str) -> dict | None:
    """Предпочтения провайдеров для поля 'provider' OpenRouter.

    Принимает JSON-строку из .env, объект или список из app_settings.
    Пусто/некорректно -> None (авто-маршрутизация).
    """
    value = await get_setting(session, key)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            log.warning("настройка %s: некорректный JSON — предпочтение провайдеров игнорируется", key)
            return None
    if isinstance(value, list):
        return {"order": value, "allow_fallbacks": True} if value else None
    if isinstance(value, dict) and value:
        return value
    return None