"""LLM-пайплайн.

Этап 2: дедупликация по хешу содержимого + правилный предфильтр (чёрный список,
минимальная длина текста, визуальное ревью для постов с медиа и коротким текстом).
Посты в статусе PREFILTERED ждут Этап 3 (LLM-классификация и рерайт).

При старте разбираются необработанные посты со статусом NEW (бэклог),
далее новые посты берутся из очереди Redis.
"""
import asyncio

from sqlalchemy import select

from app.common.logging import setup_logging
from app.config import settings
from app.db.enums import PostStatus
from app.db.models import Post
from app.db.session import session_scope
from app.redis_client import get_redis
from app.services.prefilter import run_prefilter
from app.services.queue import PIPELINE_QUEUE

log = setup_logging("pipeline")


async def process_post(post_id: int) -> None:
    try:
        await run_prefilter(post_id)
        # Этап 3: классификация (дешёвая модель) и рерайт (сильная модель)
        # будут выполняться здесь для постов в статусе PREFILTERED.
    except Exception:  # noqa: BLE001 — пост останется в NEW, повторим при следующем старте
        log.exception("ошибка обработки поста %s", post_id)


async def pending_new_post_ids() -> list[int]:
    """Посты, созданные раньше, чем пайплайн научился их обрабатывать."""
    async with session_scope() as session:
        rows = await session.execute(
            select(Post.id).where(Post.status == PostStatus.NEW).order_by(Post.id)
        )
        return list(rows.scalars().all())


async def main() -> None:
    if not settings.openrouter_api_key:
        log.warning("OPENROUTER_API_KEY не задан — LLM-этапы (Этап 3) не запустятся")
    log.info(
        "pipeline запущен (Этап 2: дедупликация + предфильтр); предохранители: бюджет $%.2f/день, не более %d кандидатов/день",
        settings.max_llm_budget_usd_per_day,
        settings.max_candidates_per_day,
    )

    # Бэклог: ваши посты из Этапа 1 ещё в статусе NEW
    pending = await pending_new_post_ids()
    if pending:
        log.info("бэклог: %d необработанных постов", len(pending))
    for post_id in pending:
        await process_post(post_id)

    # Основной цикл: очередь от reader'а
    redis = get_redis()
    while True:
        raw = await redis.lpop(PIPELINE_QUEUE)
        if raw is None:
            await asyncio.sleep(5)
            continue
        try:
            post_id = int(raw)
        except ValueError:
            log.warning("не числовой элемент в очереди, пропущен: %r", raw)
            continue
        await process_post(post_id)


if __name__ == "__main__":
    asyncio.run(main())