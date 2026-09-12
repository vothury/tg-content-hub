"""Семантическая дедупликация: каноническая форма текста + pHash медиа."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.enums import EventActor, PostStatus
from app.db.models import MediaItem, Post, PostEvent
from app.db.session import session_scope
from app.services.settings import Keys, get_setting

log = logging.getLogger("dedup")


MEDIA_TEXT_FLOOR = 0.20  # минимальное совпадение канонов, чтобы считать media-матч дублем


def _ngrams(text: str, n: int = 4):
    t = "".join(ch.lower() for ch in text if ch.isalnum() or ch == " ")
    t = " ".join(t.split())
    return [t[i:i + n] for i in range(len(t) - n + 1)]


def _cosine(a: str, b: str) -> float:
    ca, cb = Counter(_ngrams(a)), Counter(_ngrams(b))
    if not ca or not cb:
        return 0.0
    inter = sum((ca & cb).values())
    return inter / ((sum(ca.values()) * sum(cb.values())) ** 0.5)


def _hamming(a: int, b: int) -> int:
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


def _containment(a: str, b: str) -> float:
    """Доля n-грамм короткого канона, содержащихся в длинном (ловит «один список полнее другого»)."""
    ca, cb = Counter(_ngrams(a)), Counter(_ngrams(b))
    if not ca or not cb:
        return 0.0
    inter = sum((ca & cb).values())
    return inter / min(sum(ca.values()), sum(cb.values()))


def _neg_delta(a: str, b: str) -> int:
    return abs(a.count("не ") - b.count("не "))


async def _confirm_same(a: str, b: str) -> bool:
    """Дешёвый вызов: та же новость с той же полярностью, или отрицание/отмена."""
    from app.config import settings
    from app.services.llm.openrouter import chat_completion
    from app.services.llm.prompts import DEDUP_CONFIRM_SYSTEM, DEDUP_CONFIRM_USER
    from app.services.llm.schemas import DedupConfirmResult
    async with session_scope() as session:
        model = str(await get_setting(session, Keys.PREFILTER_MODEL))
    messages = [
        {"role": "system", "content": DEDUP_CONFIRM_SYSTEM},
        {"role": "user", "content": DEDUP_CONFIRM_USER.format(a=a, b=b)},
    ]
    try:
        resp = await chat_completion(messages, model, max_tokens=100, temperature=0.0)
        return bool(DedupConfirmResult.from_response(resp.content).same)
    except Exception:  # noqa: BLE001 — сбой подтверждения не ломает дедуп
        log.warning("dedup-confirm не ответил — оставляем лексическое решение")
        return True


async def run_semantic_dedup(post_id: int) -> bool:
    """True, если пост помечен DEDUPLICATED (первый пост выигрывает)."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None or post.status is not PostStatus.CANDIDATE:
            return False
        window = int(await get_setting(session, Keys.DEDUP_WINDOW_DAYS))
        ph_max = int(await get_setting(session, Keys.DEDUP_PHASH_MAX_DISTANCE))
        min_len = int(await get_setting(session, Keys.DEDUP_CANONICAL_MIN_LEN))
        cos_min = float(await get_setting(session, Keys.DEDUP_CANONICAL_COSINE_MIN))
        cont_min = float(await get_setting(session, Keys.DEDUP_CANONICAL_CONTAINMENT_MIN))
        max_cmp = int(await get_setting(session, Keys.DEDUP_MAX_COMPARE))

        since = datetime.now(timezone.utc) - timedelta(days=window)
        candidates = (await session.execute(
            select(Post)
            .where(Post.id != post_id,
                   Post.target_channel_id == post.target_channel_id,
                   Post.created_at >= since,
                   Post.status != PostStatus.DEDUPLICATED)
            .order_by(Post.id.desc()).limit(max_cmp)
        )).scalars().all()

        ids = [post_id] + [c.id for c in candidates]
        ph_map: dict[int, list[int]] = {}
        for m in (await session.execute(
                select(MediaItem).where(MediaItem.post_id.in_(ids)))).scalars().all():
            if m.phash is not None:
                ph_map.setdefault(m.post_id, []).append(m.phash)

        new_ph = ph_map.get(post_id, [])
        new_canon = (post.canonical_text or "").strip()
        dup_of = None
        reason = None
        best_cos = 0.0
        best_cont = 0.0
        best_ph = None
        cleared = None
        for c in candidates:
            c_ph = ph_map.get(c.id, [])
            c_canon = (c.canonical_text or "").strip()
            if new_ph and c_ph:
                d = min(_hamming(a, b) for a in new_ph for b in c_ph)
                best_ph = d if best_ph is None else min(best_ph, d)
                if d <= ph_max:
                    texts_ok = (
                        len(new_canon) < min_len or len(c_canon) < min_len
                        or _cosine(new_canon, c_canon) >= MEDIA_TEXT_FLOOR
                    )
                    if texts_ok:
                        nd = _neg_delta(new_canon, c_canon)
                        if nd >= 2:
                            cleared = {"candidate": c.id, "neg_delta": nd,
                                       "confirm_same": None, "rejected_by": "negation"}
                        elif await _confirm_same(new_canon, c_canon):
                            dup_of, reason = c.id, "media"
                        else:
                            cleared = {"candidate": c.id, "neg_delta": nd,
                                       "confirm_same": False, "rejected_by": "confirm"}
            if len(new_canon) >= min_len and len(c_canon) >= min_len:
                cos = _cosine(new_canon, c_canon)
                cont = _containment(new_canon, c_canon)
                best_cos = max(best_cos, cos)
                best_cont = max(best_cont, cont)
                if dup_of is None and (cos >= cos_min or cont >= cont_min):
                    nd = _neg_delta(new_canon, c_canon)
                    if nd >= 2:
                        cleared = {"candidate": c.id, "cos": round(cos, 3), "cont": round(cont, 3),
                                   "neg_delta": nd, "confirm_same": None, "rejected_by": "negation"}
                    elif await _confirm_same(new_canon, c_canon):
                        dup_of, reason = c.id, "canonical"
                    else:
                        cleared = {"candidate": c.id, "cos": round(cos, 3), "cont": round(cont, 3),
                                   "neg_delta": nd, "confirm_same": False, "rejected_by": "confirm"}
            if dup_of is not None:
                break

        post.dedup_info = {
            "dup_of": dup_of,
            "reason": reason,
            "best_canonical_sim": round(best_cos, 3),
            "best_canonical_containment": round(best_cont, 3),
            "best_phash_distance": best_ph,
            "candidates": len(candidates),
            "thresholds": {
                "cosine": cos_min, "containment": cont_min, "phash": ph_max,
                "min_len": min_len, "window_days": window,
            },
            "cleared": cleared,
        }
        if dup_of is None:
            if cleared is not None:
                session.add(PostEvent(
                    post_id=post_id, actor=EventActor.SYSTEM, action="dedup_cleared",
                    from_status=PostStatus.CANDIDATE.value, to_status=PostStatus.CANDIDATE.value,
                    details=cleared,
                ))
            await session.commit()
            return False

        post.status = PostStatus.DEDUPLICATED
        session.add(PostEvent(
            post_id=post_id, actor=EventActor.SYSTEM, action="deduplicated",
            from_status=PostStatus.CANDIDATE.value, to_status=PostStatus.DEDUPLICATED.value,
            details={"dup_of": dup_of, "reason": reason},
        ))
        await session.commit()
    log.info("пост %s: дубликат поста %s (%s)", post_id, dup_of, reason)
    # Дубль никогда не публикуется — оригиналы медиа удаляем (phash остаётся в БД для дедупа).
    # При ложном срабатывании владелец вернёт пост кнопкой «Вернуть в работу» — медиа перескачаются.
    from app.services.publishing import purge_post_media
    await purge_post_media(post_id)
    return True