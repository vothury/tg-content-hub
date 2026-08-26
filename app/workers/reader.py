"""Source Reader.

Этап 0: заглушка. Этап 1: подключение через Telethon под отдельным аккаунтом,
чтение внешних источников и тестового канала одним механизмом, скачивание медиа,
сохранение в PostgreSQL, постановка задач в Redis.
"""
import asyncio

from app.common.logging import setup_logging
from app.config import settings

log = setup_logging("reader")


async def main() -> None:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        log.warning("TELEGRAM_API_ID/API_HASH не заданы — reader в простое (заглушка этапа 0)")
    log.info("reader запущен (заглушка этапа 0)")
    while True:
        await asyncio.sleep(settings.reader_poll_interval_sec)
        log.debug("reader: heartbeat")


if __name__ == "__main__":
    asyncio.run(main())