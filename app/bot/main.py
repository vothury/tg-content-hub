"""Бот ревью.

Этап 0: заглушка. Этап 4: aiogram 3 (лонг-поллинг), карточка кандидата
с оригиналом/черновиком/медиа/оценкой, действия: готово / отклонить /
правка ИИ / редактор; доступ только для ALLOWED_OWNER_IDS.
"""
import asyncio

from app.common.logging import setup_logging
from app.config import settings

log = setup_logging("bot")


async def main() -> None:
    if not settings.bot_token:
        log.warning("BOT_TOKEN не задан — бот в простое (заглушка этапа 0)")
    if not settings.allowed_owner_ids:
        log.warning("ALLOWED_OWNER_IDS пуст — некому выполнять действия ревью")
    log.info("bot запущен (заглушка этапа 0)")
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())