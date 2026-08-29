"""LLM-пайплайн.

Этап 1: дренирует очередь новых постов из Redis, чтобы она не росла.
Полноценная обработка (предфильтр, классификация, рерайт, карточка ревью,
предохранители) — Этап 3.
"""
import asyncio

from app.common.logging import setup_logging
from app.config import settings
from app.redis_client import get_redis
from app.services.queue import PIPELINE_QUEUE

log = setup_logging("pipeline")


async def main() -> None:
    if not settings.openrouter_api_key:
        log.warning("OPENROUTER_API_KEY не задан — обработка в Этапе 3 не запустится")
    log.info(
        "pipeline запущен (Этап 1: очередь дренируется); предохранители: бюджет $%.2f/день, не более %d кандидатов/день",
        settings.max_llm_budget_usd_per_day,
        settings.max_candidates_per_day,
    )
    redis = get_redis()
    while True:
        raw = await redis.lpop(PIPELINE_QUEUE)
        if raw is None:
            await asyncio.sleep(5)
            continue
        log.info("получен пост %s — LLM-обработка появится в Этапе 3", raw)


if __name__ == "__main__":
    asyncio.run(main())