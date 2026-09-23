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
    LLM_REASONING_MAX_TOKENS = "llm.reasoning_max_tokens"
    LLM_REASONING_REWRITE = "llm.reasoning_rewrite"
    LLM_REASONING_ONLINE_CHECK = "llm.reasoning_online_check"
    LLM_REASONING_SMALL = "llm.reasoning_small"
    LLM_DOUBLE_CHECK_MAX_TOKENS = "llm.double_check_max_tokens"
    CLEAN_FALLBACK_MODEL = "llm.clean_fallback_model"
    LLM_FALLBACK_MODELS = "llm.fallback_models"
    PRICE_WATCH_ENABLED = "price_watch.enabled"
    PRICE_ALERT_PCT = "price_watch.alert_pct"
    PRICE_WATCH_INTERVAL_HOURS = "price_watch.interval_hours"
    PRICE_HISTORY_MIN_CHANGE_PCT = "price_watch.history_min_change_pct"
    PUBLISH_DUP_RECAP_WINDOW_HOURS = "publish.dup_recap_window_hours"
    PUBLISH_RESTORE_LINKS = "publish.restore_links"
    PUBLISH_MAX_MEDIA_MB = "publish.max_media_mb"
    PUBLISH_MEDIA_COMPRESS = "publish.media_compress"
    PUBLISH_COMPRESS_TARGET_MB = "publish.compress_target_mb"
    PUBLISH_COMPRESS_MAX_SIDE = "publish.compress_max_side"
    PUBLISH_SKIP_OVERSIZED = "publish.skip_oversized"
    EDITORIAL_ENABLED = "editorial.enabled"
    EDITORIAL_CYCLE_TIMES = "editorial.cycle_times"
    EDITORIAL_PUBLISH_WINDOWS = "editorial.publish_windows"
    EDITORIAL_PREPARED_HOURS = "editorial.prepared_hours"
    EDITORIAL_PUBLISHED_HOURS = "editorial.published_hours"
    EDITORIAL_WRITER_MODE = "editorial.writer_mode"
    EDITORIAL_WRITER_MODEL = "editorial.writer_model"
    EDITORIAL_AUTO_PUBLISH = "editorial.auto_publish"
    EDITORIAL_POST_MAX_CHARS = "editorial.post_max_chars"
    EDITORIAL_REWRITE_MAX_PER_DAY = "editorial.rewrite_max_per_day"
    EDITORIAL_BUDGET_USD_PER_DAY = "editorial.budget_usd_per_day"
    EDITORIAL_CHIEF_MODEL = "editorial.chief_model"
    EDITORIAL_JOURNALIST_MODEL = "editorial.journalist_model"
    EDITORIAL_BROWSE_MODEL = "editorial.browse_model"
    EDITORIAL_BROWSE_ENABLED = "editorial.browse_enabled"
    AGGREGATE_ACCEPT_DEFAULT = "aggregate.accept_default"
    AGGREGATE_REJECT_DEFAULT = "aggregate.reject_default"
    PREFILTER_MIN_TEXT_LEN = "prefilter.min_text_len"
    PREFILTER_BLACKLIST_WORDS = "prefilter.blacklist_words"
    PREFILTER_SELFPROMO_PATTERNS = "prefilter.selfpromo_patterns"
    PREFILTER_EVENT_MARKERS = "prefilter.event_markers"
    PREFILTER_EVENT_DOMAINS = "prefilter.event_domains"
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
    DEDUP_LUMA_MAX_DIFF = "dedup.luma_max_diff"
    DEDUP_CONFIRM_MODEL = "dedup.confirm_model"
    DEDUP_CONFIRM_PROVIDERS = "dedup.confirm_providers"
    PUBLISH_DUP_RECAP_WINDOW_HOURS = "publish.dup_recap_window_hours"
    DEDUP_CANONICAL_MIN_LEN = "dedup.canonical_min_len"
    DEDUP_CANONICAL_COSINE_MIN = "dedup.canonical_cosine_min"
    DEDUP_CANONICAL_CONTAINMENT_MIN = "dedup.canonical_containment_min"
    DEDUP_FACT_CONTAINMENT_MIN = "dedup.fact_containment_min"
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
    Keys.LLM_REASONING_MAX_TOKENS: settings.llm_reasoning_max_tokens,
    Keys.LLM_REASONING_REWRITE: settings.llm_reasoning_rewrite,
    Keys.LLM_REASONING_ONLINE_CHECK: settings.llm_reasoning_online_check,
    Keys.LLM_REASONING_SMALL: settings.llm_reasoning_small,
    Keys.LLM_DOUBLE_CHECK_MAX_TOKENS: settings.llm_double_check_max_tokens,
    Keys.CLEAN_FALLBACK_MODEL: settings.clean_fallback_model,
    Keys.LLM_FALLBACK_MODELS: settings.llm_fallback_models,
    Keys.PRICE_WATCH_ENABLED: settings.price_watch_enabled,
    Keys.PRICE_ALERT_PCT: settings.price_alert_pct,
    Keys.PRICE_WATCH_INTERVAL_HOURS: settings.price_watch_interval_hours,
    Keys.PRICE_HISTORY_MIN_CHANGE_PCT: settings.price_history_min_change_pct,
    Keys.PUBLISH_DUP_RECAP_WINDOW_HOURS: settings.publish_dup_recap_window_hours,
    Keys.PUBLISH_RESTORE_LINKS: settings.publish_restore_links,
    Keys.PUBLISH_MAX_MEDIA_MB: settings.publish_max_media_mb,
    Keys.PUBLISH_MEDIA_COMPRESS: settings.publish_media_compress,
    Keys.PUBLISH_COMPRESS_TARGET_MB: settings.publish_compress_target_mb,
    Keys.PUBLISH_COMPRESS_MAX_SIDE: settings.publish_compress_max_side,
    Keys.PUBLISH_SKIP_OVERSIZED: settings.publish_skip_oversized,
    Keys.EDITORIAL_ENABLED: settings.editorial_enabled,
    Keys.EDITORIAL_CYCLE_TIMES: settings.editorial_cycle_times,
    Keys.EDITORIAL_PUBLISH_WINDOWS: settings.editorial_publish_windows,
    Keys.EDITORIAL_PREPARED_HOURS: settings.editorial_prepared_hours,
    Keys.EDITORIAL_PUBLISHED_HOURS: settings.editorial_published_hours,
    Keys.EDITORIAL_WRITER_MODE: settings.editorial_writer_mode,
    Keys.EDITORIAL_WRITER_MODEL: settings.editorial_writer_model,
    Keys.EDITORIAL_AUTO_PUBLISH: settings.editorial_auto_publish,
    Keys.EDITORIAL_POST_MAX_CHARS: settings.editorial_post_max_chars,
    Keys.EDITORIAL_REWRITE_MAX_PER_DAY: settings.editorial_rewrite_max_per_day,
    Keys.EDITORIAL_BUDGET_USD_PER_DAY: settings.editorial_budget_usd_per_day,
    Keys.EDITORIAL_CHIEF_MODEL: settings.editorial_chief_model,
    Keys.EDITORIAL_JOURNALIST_MODEL: settings.editorial_journalist_model,
    Keys.EDITORIAL_BROWSE_MODEL: settings.editorial_browse_model,
    Keys.EDITORIAL_BROWSE_ENABLED: settings.editorial_browse_enabled,
    Keys.AGGREGATE_ACCEPT_DEFAULT: settings.aggregate_accept_default,
    Keys.AGGREGATE_REJECT_DEFAULT: settings.aggregate_reject_default,
    Keys.PREFILTER_MIN_TEXT_LEN: settings.prefilter_min_text_len,
    Keys.PREFILTER_BLACKLIST_WORDS: settings.prefilter_blacklist_words,
    Keys.PREFILTER_SELFPROMO_PATTERNS: settings.prefilter_selfpromo_patterns,
    Keys.PREFILTER_EVENT_MARKERS: settings.prefilter_event_markers,
    Keys.PREFILTER_EVENT_DOMAINS: settings.prefilter_event_domains,
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
    Keys.DEDUP_LUMA_MAX_DIFF: settings.dedup_luma_max_diff,
    Keys.DEDUP_CONFIRM_MODEL: settings.dedup_confirm_model,
    Keys.DEDUP_CONFIRM_PROVIDERS: settings.dedup_confirm_providers,
    Keys.PUBLISH_DUP_RECAP_WINDOW_HOURS: settings.publish_dup_recap_window_hours,
    Keys.DEDUP_CANONICAL_MIN_LEN: settings.dedup_canonical_min_len,
    Keys.DEDUP_CANONICAL_COSINE_MIN: settings.dedup_canonical_cosine_min,
    Keys.DEDUP_CANONICAL_CONTAINMENT_MIN: settings.dedup_canonical_containment_min,
    Keys.DEDUP_FACT_CONTAINMENT_MIN: settings.dedup_fact_containment_min,
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