"""M1: медиа в статьях редакции — vision-гейт иллюстраций источника.

Кандидаты = уже скачанные фото постов-материалов (MediaItem с local_path, <=5 МБ,
дедуп по phash, до 6 шт). Каждый кандидат проверяется мультимодальной моделью
на релевантность тексту статьи и на водяные знаки. Режимы (editorial.media_mode):
  off     — ничего не делать;
  shadow  — вердикты основной модели и первого фолбэка пишутся в досье статьи
            и post_events (media_verdicts), ничего не прикрепляется; это неделя
            сбора датасета для бенча по разметке владельца;
  enforce — топ-N релевантных фото без водяных знаков прикрепляются к посту
            редакции новыми MediaItem-строками на тот же local_path (без копий).
Файлы не копируются и не скачиваются заново: прикрепление = ссылка на local_path.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from sqlalchemy import select

from app.db.enums import LLMStage, MediaType
from app.db.models import Article, MediaItem, Post, PostEvent
from app.db.session import session_scope
from app.services.editorial_journalist import _call_json
from app.services.llm.prompts import MEDIA_SYSTEM, MEDIA_USER
from app.services.llm.schemas import MediaCheckResult
from app.services.settings import Keys, get_setting

log = logging.getLogger("editorial_media")

_MAX_CANDIDATES = 6
_MAX_BYTES = 5_000_000
_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
         ".webp": "image/webp"}


async def _media_chain() -> str:
    async with session_scope() as session:
        main = str(await get_setting(session, Keys.EDITORIAL_MEDIA_MODEL) or "").strip()
        fb = await get_setting(session, Keys.EDITORIAL_MEDIA_FALLBACK_MODELS) or []
    fb = [str(x).strip() for x in (fb or []) if str(x).strip()]
    return ", ".join([main] + fb) if main else ", ".join(fb)


async def _first_fallback() -> str:
    async with session_scope() as session:
        fb = await get_setting(session, Keys.EDITORIAL_MEDIA_FALLBACK_MODELS) or []
    return str(fb[0]).strip() if fb else ""


def _data_url(path: str) -> str | None:
    p = Path(path)
    if not p.is_absolute():
        p = Path("/app") / p
    if not p.exists():
        return None
    mime = _MIME.get(p.suffix.lower(), "image/jpeg")
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


async def collect_candidates(session, heads) -> list:
    post_ids = [h.post_id for h in heads if h.post_id]
    if not post_ids:
        return []
    rows = (await session.execute(
        select(MediaItem).where(
            MediaItem.post_id.in_(post_ids),
            MediaItem.downloaded.is_(True),
            MediaItem.media_type == MediaType.PHOTO,
            MediaItem.local_path.isnot(None),
            MediaItem.size_bytes <= _MAX_BYTES)
        .order_by(MediaItem.post_id, MediaItem.id))).scalars().all()
    seen, out = set(), []
    for m in rows:
        key = m.phash if m.phash is not None else m.id
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


async def _vision(spec: str, text: str, path: str) -> MediaCheckResult | None:
    url = _data_url(path)
    if not url:
        return None
    try:
        return await _call_json(
            [{"role": "system", "content": MEDIA_SYSTEM},
             {"role": "user", "content": [
                 {"type": "text", "text": MEDIA_USER.format(text=text[:3000])},
                 {"type": "image_url", "image_url": {"url": url}}]}],
            spec, None, 300, MediaCheckResult, stage=LLMStage.EDITORIAL_MEDIA)
    except Exception as exc:  # noqa: BLE001
        log.warning("media: vision-вызов %s не удался: %s", spec, exc.__class__.__name__)
        return None


async def media_step(post_id: int, art_id: int, text: str, heads) -> None:
    async with session_scope() as session:
        mode = str(await get_setting(session, Keys.EDITORIAL_MEDIA_MODE) or "off").strip().lower()
        max_photos = int(await get_setting(session, Keys.EDITORIAL_MEDIA_MAX_PHOTOS) or 3)
        cands = await collect_candidates(session, heads) if mode != "off" else []
    if mode == "off":
        return
    if not cands:
        async with session_scope() as session:
            art = await session.get(Article, art_id)
            if art is not None:
                review = dict(art.review or {})
                review["media"] = {"candidates": 0, "relevant": 0, "attached": 0,
                                   "watermark": 0, "summary": "медиа: кандидатов нет"}
                art.review = review
            await session.commit()
        return
    chain = await _media_chain()
    fb = await _first_fallback()
    items, attached = [], 0
    for m in cands:
        main = await _vision(chain, text, m.local_path or "")
        second = await _vision(fb, text, m.local_path or "") if (mode == "shadow" and fb) else None
        agree = None if second is None else (main is not None and
                 bool(main.relevant) == bool(second.relevant) and
                 bool(main.watermark) == bool(second.watermark))
        items.append({"media_id": m.id, "path": m.local_path,
                      "main": None if main is None else {"relevant": main.relevant,
                          "confidence": main.confidence, "reason": main.reason,
                          "watermark": main.watermark},
                      "fb": None if second is None else {"relevant": second.relevant,
                          "reason": second.reason, "watermark": second.watermark},
                      "agree": agree})
        if mode == "enforce" and main is not None and main.relevant and not main.watermark \
                and attached < max_photos:
            async with session_scope() as session:
                session.add(MediaItem(
                    post_id=post_id, media_type=MediaType.PHOTO,
                    src_file_id=m.src_file_id, local_path=m.local_path,
                    size_bytes=m.size_bytes, width=m.width, height=m.height,
                    downloaded=True, phash=m.phash, preview_path=m.preview_path,
                    original_name=m.original_name, luma_mean=m.luma_mean,
                    group_id=f"ed{post_id}", position=attached))
                await session.commit()
            attached += 1
    rel = sum(1 for x in items if x["main"] and x["main"]["relevant"])
    wm = sum(1 for x in items if x["main"] and x["main"]["watermark"])
    agrees = [x["agree"] for x in items if x["agree"] is not None]
    agree_pct = round(100 * sum(agrees) / len(agrees)) if agrees else None
    summary = (f"медиа: кандидатов {len(items)}, релевантных {rel}, "
               f"прикреплено {attached}, водяных знаков {wm}"
               + (f", согласие моделей {agree_pct}%" if agree_pct is not None else ""))
    log.info("media: пост #%s — %s", post_id, summary)
    async with session_scope() as session:
        art = await session.get(Article, art_id)
        if art is not None:
            review = dict(art.review or {})
            review["media"] = {"candidates": len(items), "relevant": rel,
                               "attached": attached, "watermark": wm,
                               "agree": agree_pct, "summary": summary}
            art.review = review
        session.add(PostEvent(post_id=post_id, actor=__import__(
            "app.db.enums", fromlist=["EventActor"]).EventActor.SYSTEM,
            action="media_verdicts", details={"items": items, "mode": mode}))
        await session.commit()