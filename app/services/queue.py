"""Очереди в Redis. Источник истины — PostgreSQL; Redis — только транспорт."""
from app.redis_client import get_redis

# Сюда reader кладёт новые посты; пайплайн разберёт их в Этапе 3
PIPELINE_QUEUE = "pipeline:new"


async def enqueue_post(post_id: int) -> None:
    await get_redis().rpush(PIPELINE_QUEUE, str(post_id))


async def dequeue_post() -> str | None:
    return await get_redis().lpop(PIPELINE_QUEUE)