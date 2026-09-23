"""Очереди в Redis. Источник истины — PostgreSQL; Redis — только транспорт."""

import asyncio
import logging

from app.redis_client import get_redis

log = logging.getLogger("queue")

# Сюда reader кладёт новые посты; пайплайн разберёт их в Этапе 3
PIPELINE_QUEUE = "pipeline:new"


async def enqueue_post(post_id: int) -> bool:
    """Постановка в очередь с ретраем: транзиентный сбой redis не должен прерывать
    обработку источника (пост в статусе NEW подхватит рескан pipeline)."""
    for attempt in (1, 2, 3):
        try:
            await get_redis().rpush(PIPELINE_QUEUE, str(post_id))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("enqueue_post %s: попытка %d не удалась (%s: %s)",
                        post_id, attempt, exc.__class__.__name__, exc)
            if attempt < 3:
                await asyncio.sleep(2 * attempt)
    log.error("enqueue_post %s: очередь недоступна — пост подхватит рескан", post_id)
    return False


async def dequeue_post() -> str | None:
    return await get_redis().lpop(PIPELINE_QUEUE)