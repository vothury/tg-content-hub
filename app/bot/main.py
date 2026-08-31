"""Ревью-бот (Этап 4).

aiogram 3, лонг-поллинг — входящие порты не нужны. Доступ только для
ALLOWED_OWNER_IDS. Параллельно с обработчиками работает рассылка карточек
для постов, ожидающих ревью.
"""
import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.auth import OwnerOnlyMiddleware
from app.bot.handlers import cards, commands
from app.bot.notifier import card_notifier
from app.common.logging import setup_logging
from app.config import settings

log = setup_logging("bot")


def _fsm_redis_url() -> str:
    # FSM-состояния держим в отдельной БД Redis, не смешивая с очередями
    return settings.redis_url.rsplit("/", 1)[0] + "/1"


async def main() -> None:
    if not settings.bot_token:
        log.error("BOT_TOKEN не задан — бот не запускается")
        raise SystemExit(1)
    if not settings.allowed_owner_ids:
        log.warning("ALLOWED_OWNER_IDS пуст — действия ревью будут недоступны")

    storage = RedisStorage.from_url(_fsm_redis_url())
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher(storage=storage)
    dp.message.middleware(OwnerOnlyMiddleware())
    dp.callback_query.middleware(OwnerOnlyMiddleware())
    dp.include_router(commands.router)
    dp.include_router(cards.router)

    me = await bot.get_me()
    log.info("bot запущен (Этап 4): @%s", me.username)

    notifier_task = asyncio.create_task(card_notifier(bot))
    try:
        await dp.start_polling(bot)
    finally:
        notifier_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())