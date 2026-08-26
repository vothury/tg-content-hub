"""Планировщик и публикация.

Этап 0: заглушка. Этап 5: разбор publish_jobs (сейчас/очередь/расписание),
лимиты целевых каналов, идемпотентная публикация через Bot API, ретраи.
"""
import asyncio

from app.common.logging import setup_logging

log = setup_logging("scheduler")


async def main() -> None:
    log.info("scheduler запущен (заглушка этапа 0)")
    while True:
        await asyncio.sleep(30)
        log.debug("scheduler: heartbeat")


if __name__ == "__main__":
    asyncio.run(main())