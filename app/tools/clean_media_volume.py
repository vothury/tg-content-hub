"""Чистка Docker-тома медиа: удаляет оригиналы, которые больше не нужны для публикации.

Безопасные правила:
- НИКОГДА не трогаем превью: любые файлы в подпапках 'prev' (*.webp).
- Удаляем оригинал файла ТОЛЬКО если его пост НЕ требует оригинала для публикации,
  т.е. статус НЕ в {NEW, PREFILTERED, LLM_CLASSIFYING, CANDIDATE, AWAITING_REVIEW,
  DOUBLE_CHECK_REVIEW, NEEDS_MEDIA_REVIEW, NEEDS_MANUAL_REVIEW, MANUAL_EDITING, APPROVED, SCHEDULED}.
- Также удаляем «осиротевшие» файлы: есть на диске, но нет ни в одной строке MediaItem.local_path.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import select

from app.common.logging import setup_logging
from app.config import settings
from app.db.enums import PostStatus
from app.db.models import MediaItem, Post
from app.db.session import session_scope

log = setup_logging("clean_media_volume")

MEDIA_ROOT = Path(settings.media_dir)
if not MEDIA_ROOT.is_absolute():
    MEDIA_ROOT = Path("/app") / settings.media_dir

# Статусы, при которых оригинал ещё может понадобиться для публикации
KEEP_STATUSES = {
    PostStatus.NEW, PostStatus.PREFILTERED, PostStatus.LLM_CLASSIFYING,
    PostStatus.CANDIDATE, PostStatus.AWAITING_REVIEW, PostStatus.DOUBLE_CHECK_REVIEW,
    PostStatus.NEEDS_MEDIA_REVIEW, PostStatus.NEEDS_MANUAL_REVIEW, PostStatus.MANUAL_EDITING,
    PostStatus.APPROVED, PostStatus.SCHEDULED,
}


async def main() -> None:
    async with session_scope() as session:
        # 1) Множество «защищённых» путей (оригиналы постов, которым они ещё нужны)
        protected: set[str] = set()
        for rel in (await session.execute(
                select(MediaItem.local_path)
                .select_from(MediaItem)
                .join(Post, Post.id == MediaItem.post_id)
                .where(Post.status.in_(KEEP_STATUSES),
                       MediaItem.local_path.isnot(None)))).scalars().all():
            protected.add(rel)
        # 2) Все пути, о которых знает БД (для поиска сирот)
        known: set[str] = set()
        for rel in (await session.execute(
                select(MediaItem.local_path).where(MediaItem.local_path.isnot(None)))
        ).scalars().all():
            known.add(rel)

    if not MEDIA_ROOT.exists():
        log.warning("MEDIA_ROOT не существует: %s", MEDIA_ROOT)
        return

    freed = 0
    removed = 0
    kept = 0
    for p in MEDIA_ROOT.rglob("*"):
        if not p.is_file():
            continue
        # Превью не трогаем никогда
        if "prev" in p.parts:
            continue
        rel = str(p.resolve().relative_to(MEDIA_ROOT))
        if rel in protected:
            kept += 1
            continue
        # Удаляем: либо осиротевший, либо пост больше не нуждается в оригинале
        try:
            size = p.stat().st_size
            p.unlink()
            freed += size
            removed += 1
        except Exception:  # noqa: BLE001
            log.warning("не удалось удалить %s", p)
    # Подчищаем пустые папки
    for d in sorted([x for x in MEDIA_ROOT.rglob("*") if x.is_dir()], key=lambda x: -len(x.parts)):
        try:
            if d.exists() and not any(d.iterdir()) and d != MEDIA_ROOT:
                d.rmdir()
        except Exception:  # noqa: BLE001
            pass
    log.info(
        "готово: удалено %d файлов (~%.1f МБ), оставлено защищённых %d, всего известно БД %d",
        removed, freed / 1e6, kept, len(known),
    )


if __name__ == "__main__":
    asyncio.run(main())