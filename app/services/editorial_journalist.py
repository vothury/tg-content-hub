"""Фаза журналиста виртуальной редакции: единый поток заголовков (web + tg).

Принципы: не дедуплицируем web и tg между собой, не оцениваем значимость,
не принимаем редакционных решений. Только сбор и нормализация.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import settings
from app.db.models import EditorialWebSource, Headline, Post, Source
from app.db.session import session_scope
from app.redis_client import get_redis
from app.services import guards
from app.services.llm.openrouter import chat_completion
from app.services.llm.prompts import (
    JOURNALIST_TG_SYSTEM, JOURNALIST_TG_USER,
    JOURNALIST_WEB_SYSTEM, JOURNALIST_WEB_USER,
)
from app.services.llm.schemas import HeadlineListResult, HeadlineTitleResult
from app.services.settings import Keys, get_providers, get_setting
from app.services.times import owner_now

log = logging.getLogger("editorial_journalist")

MAX_PER_SOURCE = 40
HTML_LIMIT = 60000
UA = "Mozilla/5.0 (compatible; TGContentHub/1.0)"


def _h(text: str) -> str:
    return hashlib.sha256((text or "").strip().lower().encode()).hexdigest()


async def _journalist_model() -> tuple[str, dict | None]:
    async with session_scope() as session:
        model = str(await get_setting(session, Keys.EDITORIAL_JOURNALIST_MODEL)) or \
            str(await get_setting(session, Keys.PREFILTER_MODEL))
        providers = await get_providers(session, Keys.PREFILTER_PROVIDERS)
    return model, providers


async def _account(resp) -> None:
    """Расход редакции идёт и в общий бюджет, и в отдельный счётчик редакции."""
    if resp is not None and resp.cost_usd:
        await guards.add_llm_cost(resp.cost_usd)
        day = owner_now().date().isoformat()
        await get_redis().incrbyfloat(f"guard:editorial_cost:{day}", float(resp.cost_usd))


async def _call_json(messages, model, providers, max_tokens, schema):
    resp = await chat_completion(messages, model, max_tokens, temperature=0.0,
                                 provider=providers,
                                 reasoning_max_tokens=settings.llm_reasoning_small)
    await _account(resp)
    return schema.from_response(resp.content)


async def _existing() -> tuple[set, set, set]:
    async with session_scope() as session:
        urls = set((await session.execute(
            select(Headline.url).where(Headline.url.isnot(None)))).scalars().all())
        hashes = set((await session.execute(select(Headline.title_hash))).scalars().all())
        posts = set((await session.execute(
            select(Headline.post_id).where(Headline.post_id.isnot(None)))).scalars().all())
    return urls, hashes, posts


async def _fetch_web() -> int:
    added = 0
    async with session_scope() as session:
        rows = (await session.execute(
            select(EditorialWebSource.id, EditorialWebSource.name, EditorialWebSource.url)
            .where(EditorialWebSource.enabled.is_(True)))).all()
    model, providers = await _journalist_model()
    urls_seen, hashes_seen, _ = await _existing()
    for sid, name, url in rows:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                         headers={"User-Agent": UA}) as client:
                r = await client.get(url)
            if r.status_code != 200:
                log.warning("journalist: %s -> HTTP %s", name, r.status_code)
                continue
            html = r.text[:HTML_LIMIT]
        except Exception as exc:  # noqa: BLE001
            log.warning("journalist: %s -> ошибка загрузки: %s", name, exc)
            continue
        try:
            result = await _call_json(
                [{"role": "system", "content": JOURNALIST_WEB_SYSTEM},
                 {"role": "user", "content": JOURNALIST_WEB_USER.format(base_url=url, html=html)}],
                model, providers, 1500, HeadlineListResult)
        except Exception as exc:  # noqa: BLE001
            log.warning("journalist: %s -> ошибка извлечения: %s", name, exc)
            continue
        async with session_scope() as session:
            for it in result.items[:MAX_PER_SOURCE]:
                u = it["url"] or None
                if u and u in urls_seen:
                    continue
                th = _h(it["title"])
                if th in hashes_seen:
                    continue
                session.add(Headline(source_kind="web", source_name=name, url=u,
                                     title=it["title"], title_hash=th))
                if u:
                    urls_seen.add(u)
                hashes_seen.add(th)
                added += 1
            src = await session.get(EditorialWebSource, sid)
            if src is not None:
                src.last_checked_at = datetime.now(timezone.utc)
            await session.commit()
    return added


async def _fetch_tg() -> int:
    added = 0
    async with session_scope() as session:
        rows = (await session.execute(
            select(Post.id, Post.original_text, Source.username, Source.title)
            .select_from(Post).join(Source, Source.id == Post.source_id)
            .where(Source.editorial_only.is_(True),
                   Post.created_at >= datetime.now(timezone.utc) - timedelta(hours=48),
                   ~Post.id.in_(select(Headline.post_id)
                                .where(Headline.post_id.isnot(None))))
            .order_by(Post.id))).all()
    model, providers = await _journalist_model()
    _, hashes_seen, posts_seen = await _existing()
    for pid, text, uname, stitle in rows:
        if pid in posts_seen or not (text or "").strip():
            continue
        try:
            result = await _call_json(
                [{"role": "system", "content": JOURNALIST_TG_SYSTEM},
                 {"role": "user", "content": JOURNALIST_TG_USER.format(text=(text or "")[:4000])}],
                model, providers, 300, HeadlineTitleResult)
        except Exception as exc:  # noqa: BLE001
            log.warning("journalist: tg-пост %s -> ошибка: %s", pid, exc)
            continue
        th = _h(result.title)
        if th in hashes_seen:
            continue
        async with session_scope() as session:
            session.add(Headline(source_kind="tg", source_name=uname or stitle,
                                 url=None, post_id=pid, title=result.title, title_hash=th))
            await session.commit()
        hashes_seen.add(th)
        posts_seen.add(pid)
        added += 1
    return added


async def run_journalist_phase() -> None:
    async with session_scope() as session:
        enabled = int(await get_setting(session, Keys.EDITORIAL_ENABLED))
        budget = float(await get_setting(session, Keys.EDITORIAL_BUDGET_USD_PER_DAY))
    if not enabled:
        return
    day = owner_now().date().isoformat()
    spent = float(await get_redis().get(f"guard:editorial_cost:{day}") or 0)
    if spent >= budget:
        log.warning("journalist: бюджет редакции исчерпан (%.2f/%.2f) — фаза пропущена",
                    spent, budget)
        return
    web_n = await _fetch_web()
    tg_n = await _fetch_tg()
    log.info("journalist: добавлено заголовков web=%d tg=%d", web_n, tg_n)