"""Планировщик публикаций (Этап 5).

Каждые несколько секунд просматривает задачи публикации: «сейчас», очередь
и подошедшие по расписанию. Учитывает лимиты канала (день, интервал, тихие часы)
и публикует через Bot API идемпотентно.
"""
import asyncio

from aiogram import Bot

from app.common.logging import setup_logging
from app.config import settings
from app.services.publishing import process_ready_jobs

log = setup_logging("scheduler")


async def main() -> None:
    if not settings.bot_token:
        log.error("BOT_TOKEN не задан — публикации невозможны")
        raise SystemExit(1)

    bot = Bot(token=settings.bot_token)  # без parse_mode: безопасное форматирование
    log.info("scheduler запущен (Этап 5); шаг %d сек", settings.scheduler_poll_interval_sec)
    try:
        while True:
            try:
                await process_ready_jobs(bot)
            except Exception:  # noqa: BLE001
                log.exception("сбой цикла планировщика")
            await asyncio.sleep(settings.scheduler_poll_interval_sec)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())