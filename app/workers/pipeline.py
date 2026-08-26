"""LLM-пайплайн.

Этап 0: заглушка. Этапы 2–3: предфильтр, классификация, рерайт через OpenRouter,
проверка предохранителей (бюджет/день, кандидаты/день), отправка карточки на ревью.
"""
import asyncio

from app.common.logging import setup_logging
from app.config import settings

log = setup_logging("pipeline")


async def main() -> None:
    if not settings.openrouter_api_key:
        log.warning("OPENROUTER_API_KEY не задан — pipeline в простое (заглушка этапа 0)")
    log.info(
        "pipeline запущен (заглушка этапа 0); предохранители: бюджет $%.2f/день, не более %d кандидатов/день",
        settings.max_llm_budget_usd_per_day,
        settings.max_candidates_per_day,
    )
    while True:
        await asyncio.sleep(60)
        log.debug("pipeline: heartbeat")


if __name__ == "__main__":
    asyncio.run(main())