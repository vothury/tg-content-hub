"""Дедупликация по содержимому и правилный предфильтр (Этап 2).

Дешёвый этап перед дорогим LLM:
- дедупликация по хешу нормализованного текста (в т.ч. между источниками);
- чёрные списки слов (глобальный + дополнения на источник);
- минимальная длина текста; посты с медиа и коротким текстом НЕ отклоняются,
  а переводятся на визуальное ревью (NEEDS_MEDIA_REVIEW).

Все решения пишутся в post_events.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from app.db.enums import EventActor, PostStatus
from app.db.models import MediaItem, Post, PostEvent, Source
from app.db.session import session_scope
from app.services.settings import Keys, get_setting

log = logging.getLogger(__name__)


@dataclass
class PrefilterDecision:
    status: PostStatus
    action: str          # для post_events
    reason: str          # человекочитаемая причина
    details: dict = field(default_factory=dict)


def decide(normalized_text: str, has_media: bool, min_text_len: int, blacklist_words: list[str]) -> PrefilterDecision:
    """Чистые правила без БД — удобно для тестов."""
    text = normalized_text or ""

    for word in blacklist_words:
        word_norm = (word or "").casefold().strip()
        if word_norm and word_norm in text:
            return PrefilterDecision(
                status=PostStatus.UNSUITABLE,
                action="prefilter_rejected",
                reason="blacklist",
                details={"blacklist_word": word_norm},
            )

    if len(text) >= min_text_len:
        return PrefilterDecision(
            status=PostStatus.PREFILTERED,
            action="prefilter_passed",
            reason="ok",
            details={"text_len": len(text)},
        )

    # Короткий/пустой текст: с медиа — на визуальное ревью, без медиа — отсев
    if has_media:
        return PrefilterDecision(
            status=PostStatus.NEEDS_MEDIA_REVIEW,
            action="needs_media_review",
            reason="short_text_with_media",
            details={"text_len": len(text)},
        )

    return PrefilterDecision(
        status=PostStatus.UNSUITABLE,
        action="prefilter_rejected",
        reason="too_short_or_empty",
        details={"text_len": len(text)},
    )


def _human_reason(d: PrefilterDecision) -> str:
    if d.reason == "blacklist":
        return f"предфильтр: стоп-слово «{d.details.get('blacklist_word', '')}»"
    if d.reason == "too_short_or_empty":
        return f"предфильтр: слишком короткий текст ({d.details.get('text_len', 0)} симв.)"
    return d.reason


async def run_prefilter(post_id: int) -> None:
    """Обрабатывает один пост: дедуп → правила → статус + аудит. Идемпотентно."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None or post.status is not PostStatus.NEW:
            return

        # 1) Дедупликация по хешу (у постов без текста хеш NULL — пропускаем)
        if post.text_hash:
            duplicate_id = (
                await session.execute(
                    select(Post.id)
                    .where(Post.text_hash == post.text_hash, Post.id < post.id)
                    .order_by(Post.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if duplicate_id is not None:
                post.status = PostStatus.DEDUPLICATED
                session.add(PostEvent(
                    post_id=post.id,
                    actor=EventActor.SYSTEM,
                    action="deduplicated",
                    from_status=PostStatus.NEW.value,
                    to_status=PostStatus.DEDUPLICATED.value,
                    details={"duplicate_of_post_id": duplicate_id, "text_hash": post.text_hash},
                ))
                await session.commit()
                log.info("пост %s: дубликат поста %s -> DEDUPLICATED", post.id, duplicate_id)
                return

        # 2) Правила: глобальные настройки + переопределения источника (sources.filters)
        min_text_len = int(await get_setting(session, Keys.PREFILTER_MIN_TEXT_LEN))
        blacklist = [str(w) for w in (await get_setting(session, Keys.PREFILTER_BLACKLIST_WORDS) or [])]

        source = await session.get(Source, post.source_id)
        source_filters = dict(source.filters) if source and source.filters else {}
        if "min_text_len" in source_filters:
            min_text_len = int(source_filters["min_text_len"])
        blacklist.extend(str(w) for w in source_filters.get("blacklist_words", []))

        has_media = (
            await session.execute(
                select(MediaItem.id).where(MediaItem.post_id == post.id).limit(1)
            )
        ).first() is not None

        decision = decide(post.normalized_text or "", has_media, min_text_len, blacklist)

        from_status = post.status.value
        post.status = decision.status
        if decision.status is PostStatus.UNSUITABLE:
            post.verdict_reason = _human_reason(decision)
        if decision.status is PostStatus.NEEDS_MEDIA_REVIEW:
            post.needs_media_review = True
        session.add(PostEvent(
            post_id=post.id,
            actor=EventActor.SYSTEM,
            action=decision.action,
            from_status=from_status,
            to_status=decision.status.value,
            details={"reason": decision.reason, "min_text_len": min_text_len, **decision.details},
        ))
        await session.commit()
        log.info("пост %s: %s -> %s (%s)", post.id, from_status, decision.status.value, decision.reason)