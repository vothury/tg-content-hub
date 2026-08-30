"""Предохранители пайплайна: бюджет LLM/день и кандидаты/день.

Счётчики живут в Redis с датой в ключе; сброс происходит автоматически
на следующий день. Остановка по лимиту не теряет посты — они ждут в своих
статусах и подбираются периодическим ресканом.
"""
from __future__ import annotations

import logging
from datetime import date

from app.db.session import session_scope
from app.redis_client import get_redis
from app.services.settings import Keys, get_setting

log = logging.getLogger(__name__)

KEY_COST = "guard:llm_cost:{day}"
KEY_CANDIDATES = "guard:candidates:{day}"
_TTL_SEC = 48 * 3600


def _day() -> str:
    return date.today().isoformat()


async def add_llm_cost(cost_usd: float) -> None:
    if cost_usd <= 0:
        return
    key = KEY_COST.format(day=_day())
    redis = get_redis()
    await redis.incrbyfloat(key, cost_usd)
    await redis.expire(key, _TTL_SEC)


async def budget_allows() -> bool:
    spent_raw = await get_redis().get(KEY_COST.format(day=_day()))
    spent = float(spent_raw) if spent_raw else 0.0
    async with session_scope() as session:
        limit = float(await get_setting(session, Keys.MAX_LLM_BUDGET_USD_PER_DAY))
    return spent < limit


async def candidates_cap_allows() -> bool:
    count_raw = await get_redis().get(KEY_CANDIDATES.format(day=_day()))
    count = int(count_raw) if count_raw else 0
    async with session_scope() as session:
        limit = int(await get_setting(session, Keys.MAX_CANDIDATES_PER_DAY))
    return count < limit


async def inc_candidates() -> None:
    key = KEY_CANDIDATES.format(day=_day())
    redis = get_redis()
    await redis.incr(key)
    await redis.expire(key, _TTL_SEC)