"""LLM-пайплайн (Этап 3).

Конвейер: NEW -> предфильтр -> классификация -> рерайт -> ожидание ревью.
Источники работы: очередь новых постов от reader'а и периодический рескан
постов в «промежуточных» статусах (бэклог, остановленные предохранителями,
прерванные перезапуском).
"""
import asyncio
import time

from sqlalchemy import select

from app.common.logging import setup_logging
from app.config import settings
from app.db.enums import PostStatus
from app.db.models import Post
from app.db.session import session_scope
from app.redis_client import get_redis
from app.services.llm_pipeline import advance_post
from app.services.queue import PIPELINE_QUEUE
from app.services import monitor

log = setup_logging("pipeline")

RESCAN_STATUSES = (
    PostStatus.NEW,
    PostStatus.PREFILTERED,
    PostStatus.LLM_CLASSIFYING,
    PostStatus.CANDIDATE,
    PostStatus.REWRITING,
)


async def pending_post_ids(limit: int = 50) -> list[int]:
    async with session_scope() as session:
        rows = await session.execute(
            select(Post.id).where(Post.status.in_(RESCAN_STATUSES)).order_by(Post.id).limit(limit)
        )
        return list(rows.scalars().all())


async def main() -> None:
    log.info(
        "pipeline запущен (Этап 3: LLM-классификация + рерайт); модели: classify=%s, rewrite=%s; "
        "предохранители: бюджет $%.2f/день, не более %d кандидатов/день",
        settings.classify_model,
        settings.rewrite_model,
        settings.max_llm_budget_usd_per_day,
        settings.max_candidates_per_day,
    )
    if not settings.openrouter_api_key:
        log.warning("OPENROUTER_API_KEY не задан — LLM-этапы выполняться не будут")

    # Бэклог и незавершённые посты прошлых запусков
    backlog = await pending_post_ids(limit=200)
    if backlog:
        log.info("бэклог: %d пост(ов) в работе", len(backlog))
    for post_id in backlog:
        await advance_post(post_id)

    redis = get_redis()
    next_rescan = time.monotonic() + settings.pipeline_rescan_interval_sec
    while True:
        try:
            await monitor.heartbeat("pipeline")
            raw = await redis.lpop(PIPELINE_QUEUE)
            if raw is not None:
                try:
                    post_id = int(raw)
                except ValueError:
                    log.warning("не числовой элемент в очереди, пропущен: %r", raw)
                    continue
                await advance_post(post_id)
                continue
            await asyncio.sleep(5)
            if time.monotonic() >= next_rescan:
                next_rescan = time.monotonic() + settings.pipeline_rescan_interval_sec
                pending = await pending_post_ids()
                if pending:
                    log.info("рескан: %d пост(ов) в работе", len(pending))
                    for post_id in pending:
                        await advance_post(post_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — не роняем воркер на транзиторных сбоях Redis/БД
            log.exception("сбой цикла pipeline — повтор через 5 сек")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())