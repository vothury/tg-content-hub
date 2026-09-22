"""Предохранители опасных операций админки.

Полное удаление поста разрешается только из терминала (make rm_post_true)
и автоматически отключается по таймауту. Флаг намеренно НЕ выведен в веб-настройки:
единственный способ его поднять — доступ к серверу.
"""
from __future__ import annotations

import logging

from app.redis_client import get_redis

log = logging.getLogger("security")

ARM_KEY = "admin:hard_delete_armed"
ARM_TTL_SEC = 1800  # 30 минут: хватает на пакетную чистку, но не остаётся включённым навсегда


def _truthy(value) -> bool:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


async def is_hard_delete_armed() -> bool:
    """Любая ошибка или отсутствие флага трактуется как «запрещено» (fail-closed)."""
    try:
        return _truthy(await get_redis().get(ARM_KEY))
    except Exception:  # noqa: BLE001
        log.warning("не удалось прочитать предохранитель удаления — считаю выключенным")
        return False


async def set_hard_delete_armed(enabled: bool, ttl: int = ARM_TTL_SEC) -> bool:
    try:
        if enabled:
            await get_redis().set(ARM_KEY, "1", ex=max(60, ttl))
        else:
            await get_redis().set(ARM_KEY, "0")
        log.info("предохранитель полного удаления постов: %s", "ВКЛ" if enabled else "выкл")
        return enabled
    except Exception:  # noqa: BLE001
        log.exception("не удалось изменить предохранитель удаления")
        return False