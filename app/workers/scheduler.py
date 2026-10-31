"""Планировщик публикаций (Этап 5).

Каждые несколько секунд просматривает задачи публикации: «сейчас», очередь
и подошедшие по расписанию. Учитывает лимиты канала (день, интервал, тихие часы)
и публикует через Bot API идемпотентно.
"""
import asyncio

from aiogram import Bot

from app.common.logging import setup_logging
from app.config import settings
from app.services.publishing import process_ready_jobs, recover_in_progress_jobs
from app.services import monitor

log = setup_logging("scheduler")


async def main() -> None:
    if not settings.bot_token:
        log.error("BOT_TOKEN не задан — публикации невозможны")
        raise SystemExit(1)

    bot = Bot(token=settings.bot_token)  # без parse_mode: безопасное форматирование
    log.info("scheduler запущен (Этап 5); шаг %d сек", settings.scheduler_poll_interval_sec)
    await recover_in_progress_jobs()
    try:
        while True:
            try:
                await process_ready_jobs(bot)
        try:
            from app.redis_client import get_redis as _gr
            from app.services.times import owner_now as _on
            _day = _on().date().isoformat()
            if _on().hour == 4 and await _gr().get("hygiene:media_clean_day") != _day:
                proc = await asyncio.create_subprocess_exec(
                    "python", "-m", "app.tools.clean_media_volume")
                await proc.wait()
                await _gr().set("hygiene:media_clean_day", _day, ex=2 * 86400)
                log.info("scheduler: ежедневная очистка медиа-тома, код %s",
                         proc.returncode)
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduler: сбой ежедневной гигиены медиа: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("сбой цикла планировщика")
            await monitor.heartbeat("scheduler")    
            await asyncio.sleep(settings.scheduler_poll_interval_sec)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())