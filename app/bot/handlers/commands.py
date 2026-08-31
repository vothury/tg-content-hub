from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.services import review

router = Router(name="commands")

HELP_TEXT = (
    "TG Content Hub — ревью постов.\n"
    "Карточки кандидатов приходят автоматически.\n"
    "Действия на карточке: одобрить, отклонить, правка ИИ, редактор.\n"
    "/cancel — отменить текущее действие."
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    await state.clear()
    if post_id:
        await review.cancel_interactive(post_id)
    await message.answer("Действие отменено.")