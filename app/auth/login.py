"""Одноразовый интерактивный вход аккаунта-читателя.

Запуск: make login  (docker compose run --rm reader python -m app.auth.login)
Результат: файл сессии sessions/reader.session — дальше вход не нужен.
"""
import asyncio
import getpass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from app.common.logging import setup_logging
from app.config import settings

log = setup_logging("login")


async def main() -> None:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        log.error("TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы в .env")
        raise SystemExit(1)

    client = TelegramClient(
        settings.reader_session_path,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        log.info("Сессия уже авторизована (id=%s, username=%s) — вход не требуется", me.id, me.username)
        await client.disconnect()
        return

    phone = input("Номер телефона аккаунта-читателя (+31...): ").strip()
    await client.send_code_request(phone)
    code = input("Код из Telegram (пришёл сообщением в этот аккаунт): ").strip()
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = getpass.getpass("Облачный пароль (2FA): ")
        await client.sign_in(password=password)

    me = await client.get_me()
    log.info("Вход выполнен: id=%s username=%s. Сессия сохранена.", me.id, me.username)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())