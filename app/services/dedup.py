"""Семантическая дедупликация: каноническая форма текста + pHash медиа."""
from __future__ import annotations

import asyncio
import logging
import re

from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.enums import EventActor, PostStatus
from app.db.models import LLMCall, MediaItem, Post, PostEvent, TargetChannel
from app.db.session import session_scope
from app.services import guards
from app.services.settings import Keys, get_providers, get_setting


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


_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"(?<!\()(?:https?://|t\.me/|telegram\.me/)\S+")
_SIG_LINE_RE = re.compile(
    r"^\s*\W{0,3}\s*(подпис|subscribe|наш канал|источник|@[\w_]+)\b.*$", re.I)


def _text_for_confirm(post) -> str:
    """Полный текст поста для сверки: без подписей, ссылок и с ограничением длины.

    Именно ПОЛНЫЙ текст (не канон) позволяет отличить «вышел трейлер» от
    «ведро для попкорна» при одном и том же поводе.
    """
    t = (getattr(post, "original_text", None) or getattr(post, "normalized_text", None) or "").strip()
    if not t:
        return ""
    lines = []
    for ln in t.split("\n"):
        s = ln.strip()
        if not s or _SIG_LINE_RE.match(s):
            continue
        s = _MD_LINK_RE.sub("", s)
        s = _BARE_URL_RE.sub("", s).strip()
        if s:
            lines.append(s)
    return "\n".join(lines)[:1200]


async def _log_confirm_call(post_id, model, messages, resp, ok, error, same) -> None:
    from app.db.enums import LLMCallStatus, LLMStage
    from app.services.llm.prompts import DEDUP_CONFIRM_VERSION
    if post_id is None:
        return
    async with session_scope() as session:
        session.add(LLMCall(
            post_id=post_id, stage=LLMStage.DEDUP_CONFIRM, provider="openrouter", model=model,
            prompt_version=DEDUP_CONFIRM_VERSION, request={"messages": messages},
            response=({"content": resp.content, "same": same} if resp is not None else None),
            status=LLMCallStatus.OK if ok else LLMCallStatus.PARSE_ERROR,
            error=error,
            input_tokens=resp.input_tokens if resp is not None else None,
            output_tokens=resp.output_tokens if resp is not None else None,
            cost_usd=resp.cost_usd if resp is not None else None,
            latency_ms=resp.latency_ms if resp is not None else None))
        await session.commit()


async def _confirm_same(a: str, b: str, post_id: int | None = None) -> bool:
    """Сверка ПОЛНЫХ текстов: та же новость или разные факты при общем поводе.

    Модель и провайдеры настраиваются отдельно (по умолчанию — модель очистки).
    Вызов пишется в llm_calls (стадия dedup_confirm) и учитывается в бюджете LLM.
    """
    from app.config import settings
    from app.services.llm.openrouter import chat_completion
    from app.services.llm.prompts import DEDUP_CONFIRM_SYSTEM, DEDUP_CONFIRM_USER
    from app.services.llm.schemas import DedupConfirmResult
    async with session_scope() as session:
        model = str(await get_setting(session, Keys.DEDUP_CONFIRM_MODEL)) or \
            str(await get_setting(session, Keys.PREFILTER_MODEL))
        providers = await get_providers(session, Keys.DEDUP_CONFIRM_PROVIDERS)
    messages = [
        {"role": "system", "content": DEDUP_CONFIRM_SYSTEM},
        {"role": "user", "content": DEDUP_CONFIRM_USER.format(a=a, b=b)},
    ]
    last_error = None
    for attempt in (1, 2):
        try:
            resp = await chat_completion(messages, model, max_tokens=150, temperature=0.0,
                                         provider=providers,
                                         reasoning_max_tokens=settings.llm_reasoning_small)
            if resp is not None and resp.cost_usd:
                await guards.add_llm_cost(resp.cost_usd)
            same = bool(DedupConfirmResult.from_response(resp.content).same)
            await _log_confirm_call(post_id, model, messages, resp, True, None, same)
            return same
        except Exception as exc:  # noqa: BLE001 — сбой подтверждения не ломает дедуп
            last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning("dedup-confirm попытка %d не удалась (%s)", attempt, last_error)
            if attempt == 1:
                await asyncio.sleep(20)
    await _log_confirm_call(post_id, model, messages, None, False, last_error, None)
    log.warning("dedup-confirm не ответил после 2 попыток — оставляем лексическое решение")
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
        new_full = _text_for_confirm(post)
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
                    c_full = _text_for_confirm(c)
                    same = await _confirm_same(new_full, c_full, post_id)
                    confirm_info = {"candidate": c.id, "same": same,
                                    "cos": round(cos, 3), "cont": round(cont, 3),
                                    "fact": round(fact, 3),
                                    "text_a": new_full[:120], "text_b": c_full[:120]}
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
        if dup_of is not None:
            channel = (await session.get(TargetChannel, post.target_channel_id)
                       if post.target_channel_id is not None else None)
            enabled = bool(channel.dup_recap_enabled) if channel is not None else False
            window_h = int(await get_setting(session, Keys.PUBLISH_DUP_RECAP_WINDOW_HOURS))
            orig = await session.get(Post, dup_of)
            orig_dt = orig.source_published_at if orig is not None else None
            orig_is_pub = orig is not None and orig.status is PostStatus.PUBLISHED
            in_window = (orig_dt is not None
                         and (datetime.now(timezone.utc) - orig_dt) <= timedelta(hours=window_h))
            if enabled and orig_is_pub and in_window:
                # Собираем ВСЕ опубликованные посты темы в окне (хронологически)
                recap = []
                for c in candidates:
                    if c.status is not PostStatus.PUBLISHED or c.source_published_at is None:
                        continue
                    if (datetime.now(timezone.utc) - c.source_published_at) > timedelta(hours=window_h):
                        continue
                    c_canon2 = (c.canonical_text or "").strip()
                    matched = False
                    c_ph2 = ph_map.get(c.id, [])
                    if new_ph and c_ph2:
                        if min(_hamming(a, b) for a in new_ph for b in c_ph2) <= ph_max:
                            matched = True
                    if not matched and len(new_canon) >= min_len and len(c_canon2) >= min_len:
                        if (_cosine(new_canon, c_canon2) >= cos_min
                                or _containment(new_canon, c_canon2) >= cont_min
                                or _fact_sim(new_canon, c_canon2) >= fact_min):
                            matched = await _confirm_same(new_full, _text_for_confirm(c), post_id)
                    if matched:
                        recap.append((c.source_published_at, c.id))
                recap.sort(key=lambda x: x[0])
                post.recap_ids = [pid for _, pid in recap]
                post.dedup_info = {**(post.dedup_info or {}), "recap": post.recap_ids}
                session.add(PostEvent(
                    post_id=post_id, actor=EventActor.SYSTEM, action="recap_attached",
                    from_status=PostStatus.CANDIDATE.value, to_status=PostStatus.CANDIDATE.value,
                    details={"recap": post.recap_ids},
                ))
                await session.commit()
                log.info("пост %s: дубль опубликованной темы — публикуется с блоком «ранее писали» %s",
                         post_id, post.recap_ids)
                return False  # не подавляем: пост продолжит конвейер с блоком
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