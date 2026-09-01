"""Коллбеки карточек и шаги правки ИИ / редактора / отклонения."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message


from app.bot.cards import build_card_text, load_card_data
from app.bot.keyboards import targets_keyboard
from app.bot.states import ReviewSteps
from app.services import review

from app.bot.keyboards import publish_mode_keyboard, targets_keyboard
from app.config import settings
from app.db.enums import PublishMode
from app.services.publishing import create_publish_job
from app.services.times import fmt_owner, parse_scheduled


log = logging.getLogger(__name__)
router = Router(name="cards")


def _parse_callback(data: str) -> tuple[int | None, str | None, int | None]:
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "p":
        return None, None, None
    try:
        post_id = int(parts[1])
    except ValueError:
        return None, None, None
    action = parts[2]
    extra = None
    if len(parts) >= 4:
        try:
            extra = int(parts[3])
        except ValueError:
            pass
    return post_id, action, extra


async def _edit_card(callback: CallbackQuery, ok: bool, message: str) -> None:
    if callback.message is None:
        return
    prefix = "✅" if ok else "⚠️"
    try:
        await callback.message.edit_text(f"{prefix} {message}")
    except Exception:  # noqa: BLE001 — сообщение могло стать недоступным
        pass

async def _summarize_prompt(bot: Bot, chat_id: int, message_id: int | None, text: str) -> None:
    """Заменяет сообщение-подсказку на итог действия."""
    if not message_id:
        return
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception:  # noqa: BLE001 — сообщение могло стать недоступным
        pass


@router.callback_query(F.data.startswith("p:"))
async def on_action(callback: CallbackQuery, state: FSMContext) -> None:
    post_id, action, extra = _parse_callback(callback.data)
    if post_id is None or action is None:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    if action == "approve":
        result = await review.approve(post_id)
        if result.needs_target:
            keyboard = await targets_keyboard(post_id)
            if callback.message is not None:
                await callback.message.edit_text(
                    f"Пост #{post_id}: выберите целевой канал для публикации",
                    reply_markup=keyboard,
                )
            await callback.answer()
            return
        if result.ok:
            if callback.message is not None:
                try:
                    await callback.message.edit_text(
                        f"✅ Пост #{post_id}: {result.message}",
                        reply_markup=publish_mode_keyboard(post_id),
                    )
                except Exception:  # noqa: BLE001
                    pass
            await callback.answer()
            return
        await _edit_card(callback, result.ok, f"Пост #{post_id}: {result.message}")
        await callback.answer(result.message, show_alert=True)
        return

    if action == "to":
        if extra is None:
            await callback.answer("Канал не распознан", show_alert=True)
            return
        result = await review.approve(post_id, target_channel_id=extra)
        if result.ok:
            if callback.message is not None:
                try:
                    await callback.message.edit_text(
                        f"✅ Пост #{post_id}: {result.message}",
                        reply_markup=publish_mode_keyboard(post_id),
                    )
                except Exception:  # noqa: BLE001
                    pass
            await callback.answer()
            return
        await _edit_card(callback, result.ok, f"Пост #{post_id}: {result.message}")
        await callback.answer(result.message, show_alert=True)
        return

    if action == "mode":
        parts = callback.data.split(":")
        mode_raw = parts[3] if len(parts) >= 4 else ""
        if mode_raw == "schedule":
            await state.set_state(ReviewSteps.schedule_time)
            await state.update_data(
                post_id=post_id,
                card_message_id=callback.message.message_id if callback.message else None,
            )
            if callback.message is not None:
                await callback.message.edit_text(
                    f"Пост #{post_id}: отправьте время публикации — «ГГГГ-ММ-ДД ЧЧ:ММ», "
                    f"«ДД.ММ.ГГГГ ЧЧ:ММ» или просто «ЧЧ:ММ» (часовой пояс: {settings.owner_timezone}). "
                    "Отмена — /cancel"
                )
            await callback.answer()
            return
        try:
            mode = PublishMode(mode_raw)
        except ValueError:
            await callback.answer("Неизвестный режим", show_alert=True)
            return
        ok, message = await create_publish_job(post_id, mode)
        if callback.message is not None:
            try:
                await callback.message.edit_text(f"{'✅' if ok else '⚠️'} Пост #{post_id}: {message}")
            except Exception:  # noqa: BLE001
                pass
        await callback.answer(message, show_alert=not ok)
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
        await state.update_data(
            post_id=post_id,
            card_message_id=callback.message.message_id if callback.message else None,
        )
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
        await state.update_data(
            post_id=post_id,
            card_message_id=callback.message.message_id if callback.message else None,
        )
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
        await state.update_data(
            post_id=post_id,
            card_message_id=callback.message.message_id if callback.message else None,
        )
        if callback.message is not None:
            await callback.message.edit_text(
                f"Пост #{post_id}: отправьте новый текст поста целиком одним сообщением. Отмена — /cancel"
            )
        await callback.answer()
        return

    await callback.answer()


@router.message(ReviewSteps.reject_reason)
async def on_reject_reason(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    reason = (message.text or "").strip()
    if reason == "-":
        reason = ""
    result = await review.reject(post_id, reason)
    await state.clear()
    await _summarize_prompt(bot, message.chat.id, data.get("card_message_id"),
                            f"{'❌' if result.ok else '⚠️'} Пост #{post_id}: {result.message}")
    await message.answer(f"{'❌' if result.ok else '⚠️'} {result.message}")


@router.message(ReviewSteps.manual_text)
async def on_manual_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    result = await review.apply_manual_edit(post_id, message.text or "")
    await state.clear()
    await _summarize_prompt(bot, message.chat.id, data.get("card_message_id"),
                            f"{'✏️' if result.ok else '⚠️'} Пост #{post_id}: {result.message}")
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
async def on_ai_comment(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    await message.answer("🤖 Отправляю черновик модели с вашим замечанием…")
    result = await review.apply_ai_revision(post_id, message.text or "")
    await state.clear()
    await _summarize_prompt(bot, message.chat.id, data.get("card_message_id"),
                            f"{'🤖' if result.ok else '⚠️'} Пост #{post_id}: {result.message}")
    if not result.ok:
        await message.answer(f"⚠️ {result.message}")
        return
    card = await load_card_data(post_id)
    if card is not None:
        text, keyboard = build_card_text(card)
        await message.answer(f"🤖 {result.message}.\n\n{text}", reply_markup=keyboard)
    else:
        await message.answer(result.message)


@router.message(ReviewSteps.schedule_time)
async def on_schedule_time(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    scheduled_at = parse_scheduled(message.text or "")
    if scheduled_at is None:
        await message.answer(
            "Не понял время. Форматы: «2026-09-01 18:30», «01.09.2026 18:30» или «18:30». "
            "Отмена — /cancel"
        )
        return
    ok, msg = await create_publish_job(post_id, PublishMode.SCHEDULE, scheduled_at)
    await state.clear()
    if ok:
        await _summarize_prompt(bot, message.chat.id, data.get("card_message_id"),
                                f"🕒 Пост #{post_id}: запланировано на {fmt_owner(scheduled_at)}")
        await message.answer(f"🕒 Пост #{post_id}: {msg} на {fmt_owner(scheduled_at)} (ваше время)")
    else:
        await _summarize_prompt(bot, message.chat.id, data.get("card_message_id"),
                                f"⚠️ Пост #{post_id}: {msg}")
        await message.answer(f"⚠️ {msg}")