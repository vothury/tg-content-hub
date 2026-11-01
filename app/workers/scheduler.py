"""Планировщик публикаций (Этап 5).

Каждые несколько секунд просматривает задачи публикации: «сейчас», очередь
и подошедшие по расписанию. Учитывает лимиты канала (день, интервал, тихие часы)
и публикует через Bot API идемпотентно.
Плюс ежедневная (04:00 по времени владельца) очистка медиа-тома (D1)
с redis-флагом, чтобы не повторять её в тот же день.
"""
import asyncio

from aiogram import Bot

from app.common.logging import setup_logging
from app.config import settings
from app.services.publishing import process_ready_jobs, recover_in_progress_jobs
from app.services import monitor

log = setup_logging("scheduler")


async def _daily_media_clean() -> None:
    """D1: ежедневная очистка медиа-тома по защитному списку БД (в 04:00 владельца)."""
    from app.redis_client import get_redis
    from app.services.times import owner_now

    now = owner_now()
    if now.hour != 4:
        return
    day = now.date().isoformat()
    if await get_redis().get("hygiene:media_clean_day") == day:
        return
    proc = await asyncio.create_subprocess_exec(
        "python", "-m", "app.tools.clean_media_volume")
    await proc.wait()
    await get_redis().set("hygiene:media_clean_day", day, ex=2 * 86400)
    log.info("scheduler: ежедневная очистка медиа-тома, код %s", proc.returncode)


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
                    await _daily_media_clean()
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