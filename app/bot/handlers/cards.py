"""Коллбеки карточек и шаги правки ИИ / редактора / отклонения."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.cards import build_card_text, load_card_data
from app.bot.states import ReviewSteps
from app.services import review

log = logging.getLogger(__name__)
router = Router(name="cards")


def _parse_callback(data: str) -> tuple[int | None, str | None]:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "p":
        return None, None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None, None


async def _edit_card(callback: CallbackQuery, ok: bool, message: str) -> None:
    if callback.message is None:
        return
    prefix = "✅" if ok else "⚠️"
    try:
        await callback.message.edit_text(f"{prefix} {message}")
    except Exception:  # noqa: BLE001 — сообщение могло стать недоступным
        pass


@router.callback_query(F.data.startswith("p:"))
async def on_action(callback: CallbackQuery, state: FSMContext) -> None:
    post_id, action = _parse_callback(callback.data)
    if post_id is None or action is None:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    if action == "approve":
        result = await review.approve(post_id)
        await _edit_card(callback, result.ok, f"Пост #{post_id}: {result.message}")
        await callback.answer(result.message, show_alert=not result.ok)
        return

    if action == "media_ok":
        result = await review.media_approve(post_id)
        await _edit_card(callback, result.ok, f"Пост #{post_id}: {result.message}")
        await callback.answer(result.message, show_alert=not result.ok)
        return

    if action == "retry":
        result = await review.retry_manual(post_id)
        await _edit_card(callback, result.ok, f"Пост #{post_id}: {result.message}")
        await callback.answer(result.message, show_alert=not result.ok)
        return

    if action == "reject":
        await state.set_state(ReviewSteps.reject_reason)
        await state.update_data(post_id=post_id)
        if callback.message is not None:
            await callback.message.edit_text(
                f"Пост #{post_id}: отправьте причину отклонения (или «-» без причины). Отмена — /cancel"
            )
        await callback.answer()
        return

    if action == "ai":
        result = await review.start_ai_revision(post_id)
        if not result.ok:
            await callback.answer(result.message, show_alert=True)
            return
        await state.set_state(ReviewSteps.ai_comment)
        await state.update_data(post_id=post_id)
        if callback.message is not None:
            await callback.message.edit_text(
                f"Пост #{post_id}: отправьте замечание для правки ИИ одним сообщением. Отмена — /cancel"
            )
        await callback.answer()
        return

    if action == "edit":
        result = await review.start_manual_edit(post_id)
        if not result.ok:
            await callback.answer(result.message, show_alert=True)
            return
        await state.set_state(ReviewSteps.manual_text)
        await state.update_data(post_id=post_id)
        if callback.message is not None:
            await callback.message.edit_text(
                f"Пост #{post_id}: отправьте новый текст поста целиком одним сообщением. Отмена — /cancel"
            )
        await callback.answer()
        return

    await callback.answer()


@router.message(ReviewSteps.reject_reason)
async def on_reject_reason(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    reason = (message.text or "").strip()
    if reason == "-":
        reason = ""
    result = await review.reject(post_id, reason)
    await state.clear()
    await message.answer(f"{'❌' if result.ok else '⚠️'} {result.message}")


@router.message(ReviewSteps.manual_text)
async def on_manual_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    result = await review.apply_manual_edit(post_id, message.text or "")
    await state.clear()
    if not result.ok:
        await message.answer(f"⚠️ {result.message}")
        return
    card = await load_card_data(post_id)
    if card is not None:
        text, keyboard = build_card_text(card)
        await message.answer(f"✏️ {result.message}.\n\n{text}", reply_markup=keyboard)
    else:
        await message.answer(result.message)


@router.message(ReviewSteps.ai_comment)
async def on_ai_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    await message.answer("🤖 Отправляю черновик модели с вашим замечанием…")
    result = await review.apply_ai_revision(post_id, message.text or "")
    await state.clear()
    if not result.ok:
        await message.answer(f"⚠️ {result.message}")
        return
    card = await load_card_data(post_id)
    if card is not None:
        text, keyboard = build_card_text(card)
        await message.answer(f"🤖 {result.message}.\n\n{text}", reply_markup=keyboard)
    else:
        await message.answer(result.message)