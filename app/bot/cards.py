"""Карточка кандидата: медиа (бот загружает файлы заново) + текст с оценкой."""
from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from sqlalchemy import select

from app.bot.keyboards import manual_review_keyboard, media_review_keyboard, review_keyboard
from app.config import settings
from app.db.enums import MediaType, PostStatus
from app.db.models import MediaItem, Post, PostDraftVersion, Source, TargetChannel
from app.db.session import session_scope
from app.services.review import mark_card_sent

log = logging.getLogger(__name__)

SNIPPET_LIMIT = 1200
MESSAGE_LIMIT = 4000


def media_root() -> Path:
    root = Path(settings.media_dir)
    if not root.is_absolute():
        root = Path("/app") / settings.media_dir
    return root


def _snippet(text: str | None, limit: int = SNIPPET_LIMIT) -> str:
    text = (text or "").strip()
    if not text:
        return "(пусто)"
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(усечено)"


async def load_card_data(post_id: int) -> dict | None:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return None
        source = await session.get(Source, post.source_id)
        target = None
        if post.target_channel_id is not None:
            t = await session.get(TargetChannel, post.target_channel_id)
            if t is not None:
                target = {"id": t.id, "username": t.username, "title": t.title}
        media = (
            await session.execute(
                select(MediaItem).where(MediaItem.post_id == post_id).order_by(MediaItem.position)
            )
        ).scalars().all()
        last_version = (
            await session.execute(
                select(PostDraftVersion)
                .where(PostDraftVersion.post_id == post_id)
                .order_by(PostDraftVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return {
            "post_id": post_id,
            "status": post.status,
            "source_username": source.username if source else "?",
            "target": target,
            "post_url": post.post_url,
            "score": post.score,
            "verdict_reason": post.verdict_reason,
            "risks": post.risks or [],
            "original": post.original_text,
            "draft": post.draft_text,
            "draft_version": post.draft_version,
            "draft_origin": last_version.origin.value if last_version is not None else None,
            "media": [
                {"type": m.media_type, "local_path": m.local_path, "downloaded": m.downloaded}
                for m in media
            ],
        }


def build_card_text(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    status = data["status"]
    lines = [f"🆕 Кандидат #{data['post_id']} — @{data['source_username']}"]

    if data["target"]:
        lines.append(f"🎯 Канал: @{data['target']['username']} ({data['target']['title']})")
    else:
        lines.append("🎯 Канал: будет выбран при одобрении")

    if status is PostStatus.NEEDS_MEDIA_REVIEW:
        lines.append("ℹ️ Визуальный пост (медиа и короткий текст) — решение за вами.")
    elif status is PostStatus.NEEDS_MANUAL_REVIEW:
        lines.append("⚠️ Требуется ручной разбор (ошибка обработки).")

    if data["score"] is not None:
        lines.append(f"Оценка: {data['score']:g}/10")
    if data["verdict_reason"]:
        lines.append(f"Вердикт: {data['verdict_reason']}")
    if data["risks"]:
        lines.append("Риски: " + "; ".join(str(r) for r in data["risks"][:5]))

    lines.append("")
    lines.append("📄 Оригинал:")
    lines.append(_snippet(data["original"]))
    if data["draft"]:
        lines.append("")
        if data.get("draft_origin") == "original":
            lines.append("✍️ Черновик (оригинал, без рерайта):")
        else:
            lines.append(f"✍️ Черновик (v{data['draft_version']}):")
        lines.append(_snippet(data["draft"]))
    if data["post_url"]:
        lines.append("")
        lines.append(f"🔗 Источник: {data['post_url']}")

    if status is PostStatus.NEEDS_MEDIA_REVIEW:
        keyboard = media_review_keyboard(data["post_id"])
    elif status is PostStatus.NEEDS_MANUAL_REVIEW:
        keyboard = manual_review_keyboard(data["post_id"])
    else:
        keyboard = review_keyboard(data["post_id"])

    text = "\n".join(lines)
    if len(text) > MESSAGE_LIMIT:
        text = text[:MESSAGE_LIMIT] + "\n…(усечено)"
    return text, keyboard


async def send_card(bot: Bot, chat_id: int, post_id: int) -> bool:
    data = await load_card_data(post_id)
    if data is None:
        return False

    # Медиа: альбом или одиночный файл (бот загружает заново из локального тома).
    # Подпись передаётся в конструкторе: модели InputMedia* в aiogram 3 заморожены.
    files: list = []
    first = True
    root = media_root()
    for m in data["media"]:
        if not m["downloaded"] or not m["local_path"]:
            continue
        path = root / m["local_path"]
        if not path.exists():
            log.warning("пост %s: медиафайл не найден: %s", post_id, path)
            continue
        file_caption = (
            f"🆕 Кандидат #{post_id} — @{data['source_username']}" if first else None
        )
        if m["type"] is MediaType.VIDEO:
            files.append(InputMediaVideo(media=FSInputFile(path), caption=file_caption))
        else:
            files.append(InputMediaPhoto(media=FSInputFile(path), caption=file_caption))
        first = False

    if files:
        try:
            await bot.send_media_group(chat_id, media=files)
        except Exception:  # noqa: BLE001
            log.exception("пост %s: не удалось отправить медиа", post_id)

    text, keyboard = build_card_text(data)
    await bot.send_message(chat_id, text, reply_markup=keyboard)
    await mark_card_sent(post_id, data["draft_version"])
    return True