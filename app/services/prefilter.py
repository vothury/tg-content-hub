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
import re
from dataclasses import dataclass, field

from sqlalchemy import select

from app.db.enums import EventActor, PostStatus
from app.db.models import MediaItem, Post, PostEvent, Source
from app.db.session import session_scope
from app.services.settings import Keys, get_setting, is_sensitive_text, sensitive_words Keys, get_setting
from app.services.dedup import _hamming

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


_MD_LINK_PREF_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_LINK_PREF_RE = re.compile(
    r"https?://\S+|(?:t\.me|telegram\.me)/[\w+/\-]+|@[A-Za-z0-9_]{4,}", re.I)


def _is_signature_line(line: str) -> bool:
    """Похожа ли строка на подпись/футер источника: «Источник: …», ссылки на площадки, CTA."""
    s = line.strip()
    if not s:
        return False
    if re.match(r"^\W{0,3}\s*источник\s*[:—-]", s, re.I):
        return True
    if _LINK_PREF_RE.search(s):
        return True
    return len(s) <= 60 and bool(re.search(r"подпис|subscribe|наш канал", s, re.I))


_EVENT_DATE_RE = re.compile(
    r"\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|"
    r"октября|ноября|декабря)", re.I)
_EVENT_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")


def _event_promo_hit(text: str, markers, domains) -> str | None:
    """Анонс мероприятия: маркер + площадка регистрации ИЛИ дата и время проведения."""
    if not text:
        return None
    low = text.lower()
    marker = next((m for m in (markers or []) if m and m.lower() in low), None)
    if marker is None:
        return None
    domain = next((d for d in (domains or []) if d and d.lower() in low), None)
    if domain:
        return f"{marker} + площадка регистрации {domain}"
    if _EVENT_DATE_RE.search(text) and _EVENT_TIME_RE.search(text):
        return f"{marker} + дата и время проведения"
    return None


def _selfpromo_hit(text: str, patterns) -> str | None:
    """Маркер самопиара в КОНТЕНТЕ поста.

    Совпадение в одной из двух последних строк, если эта строка — подпись источника,
    НЕ считается самопиаром: такие строки снимает очистка подписей, пост публикуется.
    """
    if not text or not patterns:
        return None
    lines = text.split("\n")
    nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
    tail = set(nonempty[-2:]) if nonempty else set()
    for i in nonempty:
        low = lines[i].lower()
        for p in patterns:
            if not p:
                continue
            if p.lower() in low:
                if i in tail and _is_signature_line(lines[i]):
                    continue
                return p
    return None


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
                    .where(Post.text_hash == post.text_hash,
                           Post.target_channel_id == post.target_channel_id,
                           Post.id < post.id)
                    .order_by(Post.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if duplicate_id is not None:
                min_len = int(await get_setting(session, Keys.DEDUP_CANONICAL_MIN_LEN))
                norm_len = len((post.normalized_text or "").strip())
                compatible = norm_len >= min_len
                if not compatible:
                    # Текст тривиален («🙂»): требуем совместимости медиа, иначе это не дубль
                    ph_max = int(await get_setting(session, Keys.DEDUP_PHASH_MAX_DISTANCE))
                    ph_new = list((await session.execute(
                        select(MediaItem.phash).where(
                            MediaItem.post_id == post.id,
                            MediaItem.phash.isnot(None)))).scalars().all())
                    ph_old = list((await session.execute(
                        select(MediaItem.phash).where(
                            MediaItem.post_id == duplicate_id,
                            MediaItem.phash.isnot(None)))).scalars().all())
                    if not ph_new and not ph_old:
                        compatible = True
                    elif ph_new and ph_old and any(
                            _hamming(a, b) <= ph_max for a in ph_new for b in ph_old):
                        compatible = True
                if compatible:
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
                # иначе: совпадение хеша при тривиальном тексте и разных медиа — не дубль;
                # решение примет семантическая дедупликация после классификации

        # 2) Правила: глобальные настройки + переопределения источника (sources.filters)
        min_text_len = int(await get_setting(session, Keys.PREFILTER_MIN_TEXT_LEN))
        blacklist = [str(w) for w in (await get_setting(session, Keys.PREFILTER_BLACKLIST_WORDS) or [])]

        source = await session.get(Source, post.source_id)
        source_filters = dict(source.filters) if source and source.filters else {}
        if source_filters.get("min_text_len") is not None:
            min_text_len = int(source_filters["min_text_len"])
        if source_filters.get("blacklist_words") is not None:
            blacklist = [str(w) for w in source_filters["blacklist_words"]]
        max_text_len = source_filters.get("max_text_len")
        if max_text_len is not None:
            norm_len = len((post.normalized_text or "").strip())
            if norm_len > int(max_text_len):
                post.status = PostStatus.UNSUITABLE
                post.verdict_reason = (f"технический лимит источника: "
                                       f"длина {norm_len} > {int(max_text_len)}")
                session.add(PostEvent(
                    post_id=post.id, actor=EventActor.SYSTEM, action="prefilter_rejected",
                    from_status=PostStatus.NEW.value, to_status=PostStatus.UNSUITABLE.value,
                    details={"reason": "max_text_len", "len": norm_len,
                             "max": int(max_text_len)},
                ))
                await session.commit()
                log.info("пост %s: отсечён по max_text_len (%d > %d)",
                         post.id, norm_len, int(max_text_len))
                return
                
        # Технический стоп-фильтр самопиара источника: «залил нам на канал», «у нас на канале» и т.п.
        promo_patterns = await get_setting(session, Keys.PREFILTER_SELFPROMO_PATTERNS)
        if isinstance(promo_patterns, str):
            promo_patterns = [p.strip() for p in promo_patterns.split(",") if p.strip()]
        hit = _selfpromo_hit(post.original_text or post.normalized_text or "",
                             promo_patterns or [])
        if hit:
            post.status = PostStatus.UNSUITABLE
            post.verdict_reason = f"самопиар источника: «{hit}»"
            session.add(PostEvent(
                post_id=post.id, actor=EventActor.SYSTEM, action="selfpromo_blocked",
                from_status=PostStatus.NEW.value, to_status=PostStatus.UNSUITABLE.value,
                details={"pattern": hit, "note": "маркер в контенте, не в подписи"},
            ))
            await session.commit()
            log.info("пост %s: отсечён технически — самопиар источника (%s)", post.id, hit)
            return

        event_markers = await get_setting(session, Keys.PREFILTER_EVENT_MARKERS)
        event_domains = await get_setting(session, Keys.PREFILTER_EVENT_DOMAINS)
        if isinstance(event_markers, str):
            event_markers = [x.strip() for x in event_markers.split(",") if x.strip()]
        if isinstance(event_domains, str):
            event_domains = [x.strip() for x in event_domains.split(",") if x.strip()]
        ev = _event_promo_hit(post.original_text or post.normalized_text or "",
                              event_markers, event_domains)
        if ev:
            post.status = PostStatus.UNSUITABLE
            post.verdict_reason = f"анонс мероприятия: {ev}"
            session.add(PostEvent(
                post_id=post.id, actor=EventActor.SYSTEM, action="event_promo_blocked",
                from_status=PostStatus.NEW.value, to_status=PostStatus.UNSUITABLE.value,
                details={"hit": ev},
            ))
            await session.commit()
            log.info("пост %s: отсечён технически — анонс мероприятия (%s)", post.id, ev)
            return

        # Чувствительная лексика: провайдеры обрывают рассуждения на таких словах.
        # Без заданной llm_sensitive_model семантику не запускаем — только ручное решение.
        words = await sensitive_words(session)
        if is_sensitive_text(post.normalized_text or post.original_text or "", words):
            post.sensitive = True
            sm = str(await get_setting(session, Keys.LLM_SENSITIVE_MODEL) or "").strip()
            if not sm:
                post.status = PostStatus.NEEDS_MANUAL_REVIEW
                post.verdict_reason = ("чувствительная лексика: семантика отключена — "
                                       "решите вручную или задайте «LLM: модель для чувствительных постов»")
                session.add(PostEvent(
                    post_id=post.id, actor=EventActor.SYSTEM, action="sensitive_manual",
                    from_status=PostStatus.NEW.value,
                    to_status=PostStatus.NEEDS_MANUAL_REVIEW.value))
                await session.commit()
                log.info("пост %s: чувствительная лексика — только ручное решение", post.id)
                return
            session.add(PostEvent(
                post_id=post.id, actor=EventActor.SYSTEM, action="sensitive_route",
                details={"model": sm}))
            log.info("пост %s: чувствительная лексика — семантика через %s", post.id, sm)

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