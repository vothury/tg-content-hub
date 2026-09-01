from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.cards import build_card_text, load_card_data, send_card
from app.bot.keyboards import publish_mode_keyboard
from app.db.enums import PostStatus
from app.services import review

router = Router(name="commands")

HELP_TEXT = (
    "TG Content Hub — ревью постов.\n"
    "Карточки кандидатов приходят автоматически.\n"
    "Действия на карточке: одобрить, отклонить, правка ИИ, редактор.\n"
    "/card <номер> — прислать карточку поста заново (например, /card 45).\n"
    "/cancel — отменить текущее действие и восстановить карточку."
)

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    card_message_id = data.get("card_message_id")
    await state.clear()
    if post_id:
        await review.cancel_interactive(post_id)

    if not post_id:
        await message.answer("Нет действия для отмены.")
        return

    card = await load_card_data(post_id)
    if card is None:
        await message.answer("Действие отменено.")
        return

    text, keyboard = build_card_text(card)
    if card["status"] is PostStatus.APPROVED:
        keyboard = publish_mode_keyboard(post_id)

    restored = False
    if card_message_id:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id, message_id=card_message_id,
                text=text, reply_markup=keyboard,
            )
            restored = True
        except Exception:  # noqa: BLE001 — сообщение могло стать недоступным
            pass

    if restored:
        await message.answer("Действие отменено, карточка восстановлена.")
    else:
        await message.answer(f"Действие отменено. Карточка поста #{post_id}:", reply_markup=keyboard)


@router.message(Command("card"))
async def cmd_card(message: Message, bot: Bot) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /card <номер поста>, например /card 45")
        return
    post_id = int(parts[1])
    card = await load_card_data(post_id)
    if card is None:
        await message.answer(f"Пост #{post_id} не найден")
        return
    await send_card(bot, message.chat.id, post_id)