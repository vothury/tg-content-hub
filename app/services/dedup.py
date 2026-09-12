"""Семантическая дедупликация: каноническая форма текста + pHash медиа."""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.enums import EventActor, PostStatus
from app.db.models import MediaItem, Post, PostEvent
from app.db.session import session_scope
from app.services.settings import Keys, get_setting


log = logging.getLogger("dedup")


MEDIA_TEXT_FLOOR = 0.20  # минимальное совпадение канонов, чтобы считать media-матч дублем


_FACT_QUOTED = re.compile(r"«([^»]+)»")
_FACT_NAME = re.compile(r"[А-ЯЁA-Z][а-яёa-z]+(?: [А-ЯЁA-Z][а-яёa-z]+){0,2}")
_FACT_DATE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2} [а-яё]+ \d{4}|\d{4}")


def _fact_tokens(text: str) -> set[str]:
    toks = {("«" + q.strip() + "»") for q in _FACT_QUOTED.findall(text.lower())}
    toks |= {n.lower() for n in _FACT_NAME.findall(text)}
    toks |= {d.strip() for d in _FACT_DATE.findall(text.lower())}
    return toks


def _fact_sim(a: str, b: str) -> float:
    ta, tb = _fact_tokens(a), _fact_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


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
        fact_min = float(await get_setting(session, Keys.DEDUP_FACT_CONTAINMENT_MIN))
        luma_max = int(await get_setting(session, Keys.DEDUP_LUMA_MAX_DIFF))
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
        lm_map: dict[int, list[int]] = {}
        for pid, ph, lm in (await session.execute(
                select(MediaItem.post_id, MediaItem.phash, MediaItem.luma_mean)
                .where(MediaItem.post_id.in_(ids)))).all():
            if ph is not None:
                ph_map.setdefault(pid, []).append(ph)
            if lm is not None:
                lm_map.setdefault(pid, []).append(lm)

        new_ph = ph_map.get(post_id, [])
        new_canon = (post.canonical_text or "").strip()
        dup_of = None
        reason = None
        best_cos = 0.0
        best_cont = 0.0
        best_fact = 0.0
        best_ph = None
        confirm_info = None
        for c in candidates:
            c_ph = ph_map.get(c.id, [])
            c_canon = (c.canonical_text or "").strip()
            # Медиа-матч: самостоятельное доказательство; текстовое подтверждение не применимо.
            # Guard от коллизий dHash — непротиворечивость текстов (MEDIA_TEXT_FLOOR).
            if new_ph and c_ph:
                d = min(_hamming(a, b) for a in new_ph for b in c_ph)
                best_ph = d if best_ph is None else min(best_ph, d)
                if d <= ph_max:
                    # Тексты: канон, а при его отсутствии — исходный текст (якоря/косинус).
                    eff_new = new_canon if len(new_canon) >= min_len else (post.normalized_text or "")
                    eff_c = c_canon if len(c_canon) >= min_len else (c.normalized_text or "")
                    texts_ok = (
                        eff_new.strip() == eff_c.strip()
                        or _cosine(eff_new, eff_c) >= MEDIA_TEXT_FLOOR
                        or _fact_sim(eff_new, eff_c) >= MEDIA_TEXT_FLOOR
                    )
                    # Яркость: чёрное и белое не могут быть одним изображением.
                    lm_new = lm_map.get(post_id, [])
                    lm_c = lm_map.get(c.id, [])
                    luma_ok = (not lm_new or not lm_c) or any(
                        abs(a - b) <= luma_max for a in lm_new for b in lm_c)
                    if texts_ok and luma_ok:
                        dup_of, reason = c.id, "media"
            # Текстовый матч: ВСЕГДА подтверждаем моделью (отрицания, отмены, обновления и т.п.).
            if dup_of is None and len(new_canon) >= min_len and len(c_canon) >= min_len:
                cos = _cosine(new_canon, c_canon)
                cont = _containment(new_canon, c_canon)
                fact = _fact_sim(new_canon, c_canon)
                best_cos = max(best_cos, cos)
                best_cont = max(best_cont, cont)
                best_fact = max(best_fact, fact)
                if cos >= cos_min or cont >= cont_min or fact >= fact_min:
                    same = await _confirm_same(new_canon, c_canon)
                    confirm_info = {"candidate": c.id, "same": same,
                                    "cos": round(cos, 3), "cont": round(cont, 3),
                                    "fact": round(fact, 3)}
                    if same:
                        dup_of, reason = c.id, "canonical"
            if dup_of is not None:
                break

        post.dedup_info = {
            "dup_of": dup_of,
            "reason": reason,
            "best_canonical_sim": round(best_cos, 3),
            "best_canonical_containment": round(best_cont, 3),
            "best_fact_containment": round(best_fact, 3),
            "best_phash_distance": best_ph,
            "candidates": len(candidates),
            "confirm": confirm_info,
            "thresholds": {
                "cosine": cos_min, "containment": cont_min, "fact": fact_min,
                "phash": ph_max, "luma": luma_max,
                "min_len": min_len, "window_days": window,
            },
        }
        if dup_of is None:
            if confirm_info is not None:
                session.add(PostEvent(
                    post_id=post_id, actor=EventActor.SYSTEM, action="dedup_cleared",
                    from_status=PostStatus.CANDIDATE.value, to_status=PostStatus.CANDIDATE.value,
                    details=confirm_info,
                ))
            await session.commit()
            return False

        post.status = PostStatus.DEDUPLICATED
        session.add(PostEvent(
            post_id=post_id, actor=EventActor.SYSTEM, action="deduplicated",
            from_status=PostStatus.CANDIDATE.value, to_status=PostStatus.DEDUPLICATED.value,
            details={"dup_of": dup_of, "reason": reason, "confirm": confirm_info},
        ))
        await session.commit()
    log.info("пост %s: дубликат поста %s (%s)", post_id, dup_of, reason)
    from app.services.publishing import purge_post_media
    await purge_post_media(post_id)
    return True