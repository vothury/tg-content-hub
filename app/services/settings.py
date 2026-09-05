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
    CLASSIFY_VERBOSE = "llm.classify_verbose"


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
    Keys.CLASSIFY_VERBOSE: settings.classify_verbose,
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

    Принимает JSON-строку из .env или объект из app_settings.
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
    if isinstance(value, dict) and value:
        return value
    return None