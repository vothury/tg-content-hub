"""Фаза журналиста виртуальной редакции: единый поток заголовков (web + tg)."""
from __future__ import annotations  # ИСПРАВЛЕНО: __future__

import hashlib
import logging
import xml.etree.ElementTree as ET  # ДОБАВЛЕНО
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin

import httpx
from bs4 import BeautifulSoup  # ДОБАВЛЕНО
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
    JOURNALIST_BROWSE_SYSTEM, JOURNALIST_BROWSE_USER,  # ДОБАВЛЕНО
)
from app.services.llm.schemas import (
    HeadlineListResult, HeadlinePickResult, HeadlineTitleResult,
)
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


# --- ВОССТАНОВЛЕННЫЕ ХЕЛПЕРЫ (были пропущены) ---

def _parse_feed(text: str, limit: int):
    """RSS 2.0 / Atom -> [(title, url)]."""
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            out.append((title, link))
            if len(out) >= limit:
                break
    if not out:
        ns = "{http://www.w3.org/2005/Atom}"
        for entry in root.iter(f"{ns}entry"):
            title = " ".join((entry.findtext(f"{ns}title") or "").split())
            link = ""
            for l in entry.findall(f"{ns}link"):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href") or ""
                    break
            if title and link:
                out.append((title, link))
                if len(out) >= limit:
                    break
    return out


def _parse_selectors(html: str, base_url: str, s_list: str, s_title, s_link, limit: int):
    """CSS-селекторы -> [(title, url)]."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen = set()
    for node in soup.select(s_list):
        tnode = node.select_one(s_title) if s_title else node
        anode = node.select_one(s_link) if s_link else \
            (node if node.name == "a" else node.find("a"))
        if tnode is None or anode is None or not anode.get("href"):
            continue
        title = " ".join(tnode.get_text(" ", strip=True).split())
        if not (12 <= len(title) <= 250):
            continue
        url, _ = urldefrag(urljoin(base_url, anode["href"]))
        if url in seen:
            continue
        seen.add(url)
        out.append((title, url))
        if len(out) >= limit:
            break
    return out


_NAV_HINTS = ("/ad", "subscribe", "podpiska", "reklama", "login", "register",
              "conference", "vote", "about", "contacts", "media", "terms")


def _clean_anchors(anchors):
    out = []
    for text, url in anchors:
        low = url.lower()
        if any(h in low for h in _NAV_HINTS):
            continue
        if text.isupper() and len(text) < 30:
            continue
        out.append((text, url))
    return out


# --- ОСНОВНЫЕ ФУНКЦИИ ---

class _AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items: list[tuple[str, str]] = []
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []
        elif self._href is not None and tag in ("script", "style"):
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._buf).split())
            self.items.append((text, self._href))
            self._href = None
            self._buf = []


def _extract_anchors(html: str, base_url: str, limit: int = 300):
    p = _AnchorParser()
    try:
        p.feed(html)
    except Exception:
        pass
    out = []
    seen = set()
    for text, href in p.items:
        if not href or href.startswith(("#", "javascript:", "mailto:")):  # ИСПРАВЛЕНО: убраны пробелы
            continue
        text = text.strip()
        if not (12 <= len(text) <= 250):
            continue
        url, _ = urldefrag(urljoin(base_url, href))
        if url in seen:
            continue
        seen.add(url)
        out.append((text, url))
        if len(out) >= limit:
            break
    return out


async def _browse_headlines(url: str, name: str) -> list:
    async with session_scope() as session:
        model = str(await get_setting(session, Keys.EDITORIAL_BROWSE_MODEL))
        providers = await get_providers(session, Keys.PREFILTER_PROVIDERS)
        if not model:
            base = str(await get_setting(session, Keys.EDITORIAL_JOURNALIST_MODEL)) or \
                   str(await get_setting(session, Keys.PREFILTER_MODEL))
            model = base + ":online"  # ИСПРАВЛЕНО: убран пробел
    try:
        result = await _call_json(
            [{"role": "system", "content": JOURNALIST_BROWSE_SYSTEM},
             {"role": "user", "content": JOURNALIST_BROWSE_USER.format(url=url)}],
            model, providers, 1500, HeadlineListResult)
    except Exception as exc:
        log.warning("journalist: %s -> browse-модель не смогла: %s: %s",
                    name, exc.__class__.__name__, exc)  # ИСПРАВЛЕНО: exc.__class__
        return []
    out = [(it["title"], it["url"] or url) for it in result.items[:MAX_PER_SOURCE]]
    log.info("journalist: %s [browse] -> %d заголовков", name, len(out))
    return out


async def _fetch_web() -> int:  # ИСПРАВЛЕНО: имя функции с _
    added = 0
    async with session_scope() as session:
        rows = (await session.execute(
            select(EditorialWebSource.id, EditorialWebSource.name, EditorialWebSource.url,
                   EditorialWebSource.feed_url, EditorialWebSource.list_selector,
                   EditorialWebSource.title_selector, EditorialWebSource.link_selector)
            .where(EditorialWebSource.enabled.is_(True)))).all()
    model, providers = await _journalist_model()
    urls_seen, hashes_seen, _ = await _existing()

    for sid, name, url, feed_url, s_list, s_title, s_link in rows:
        items: list = []
        body: str | None = None
        strategy = "feed" if feed_url else ("selectors" if s_list else "fallback")
        used = strategy
        target = feed_url or url

        if strategy in ("feed", "selectors"):
            try:
                async with httpx.AsyncClient(timeout=45, follow_redirects=True,
                                             headers={"User-Agent": UA}) as client:
                    r = await client.get(target)
                if r.status_code != 200:
                    log.warning("journalist: %s -> HTTP %s (%s)", name, r.status_code, strategy)
                    continue
                body = r.text[:HTML_LIMIT]
            except Exception as exc:
                log.warning("journalist: %s -> ошибка загрузки (%s): %s: %s",
                            name, strategy, exc.__class__.__name__, exc)
                continue
            
            if strategy == "feed":
                items = _parse_feed(body, MAX_PER_SOURCE)
                if not items:
                    log.warning("journalist: %s -> лента не распознана", name)
                    continue
            else:
                items = _parse_selectors(body, target, s_list, s_title, s_link, MAX_PER_SOURCE)
                if not items:
                    log.warning("journalist: %s -> селекторы не дали заголовков", name)
                    continue
        else:
            # Fallback path
            try:
                async with httpx.AsyncClient(timeout=45, follow_redirects=True,
                                             headers={"User-Agent": UA}) as client:
                    r = await client.get(target)
                if r.status_code == 200:
                    body = r.text[:HTML_LIMIT]
                else:
                    log.info("journalist: %s -> HTTP %s, идём через browse", name, r.status_code)
            except Exception as exc:
                log.info("journalist: %s -> код-загрузка не удалась, идём через browse", name)
                body = None
            
            anchors = _clean_anchors(_extract_anchors(body, target)) if body else []
            if anchors:
                listing = "\n".join(f"{i}. {t}" for i, (t, u) in enumerate(anchors, 1))
                result = None
                for attempt in (1, 2):
                    try:
                        result = await _call_json(
                            [{"role": "system", "content": JOURNALIST_WEB_SYSTEM},
                             {"role": "user", "content": JOURNALIST_WEB_USER.format(listing=listing)}],
                            model, providers, 1200, HeadlinePickResult)
                        break
                    except Exception as exc:
                        if attempt == 2:
                            log.warning("journalist: %s -> ошибка извлечения: %s", name, exc)
                
                if result is not None:
                    for it in result.items[:MAX_PER_SOURCE]:
                        idx = it["i"] - 1
                        if 0 <= idx < len(anchors):
                            items.append((it["title"] or anchors[idx][0], anchors[idx][1]))
            
            if not items:
                items = await _browse_headlines(target, name)
                used = "browse"
            
            if not items:
                continue

        async with session_scope() as session:
            for title, u in items:
                if u in urls_seen:
                    continue
                th = _h(title)
                if th in hashes_seen:
                    continue
                session.add(Headline(source_kind="web", source_name=name, url=u,
                                     title=title, title_hash=th))
                urls_seen.add(u)
                hashes_seen.add(th)
                added += 1
            src = await session.get(EditorialWebSource, sid)
            if src is not None:
                src.last_checked_at = datetime.now(timezone.utc)
            await session.commit()
        log.info("journalist: %s [%s] -> %d заголовков", name, used, len(items))
    return added


async def _fetch_tg() -> int:  # ИСПРАВЛЕНО: имя функции с _
    added = 0
    async with session_scope() as session:
        rows = (await session.execute(
            select(Post.id, Post.original_text, Source.username, Source.title)
            .select_from(Post).join(Source, Source.id == Post.source_id)  # ИСПРАВЛЕНО: Source
            .where(Source.editorial_only.is_(True),
                   Post.created_at >= datetime.now(timezone.utc) - timedelta(hours=48),
                   ~Post.id.in_(select(Headline.post_id)
                                .where(Headline.post_id.isnot(None))))
            .order_by(Post.id))).all()
    
    model, providers = await _journalist_model()  # ИСПРАВЛЕНО: await
    _, hashes_seen, posts_seen = await _existing()
    
    for pid, text, uname, stitle in rows:
        if pid in posts_seen or not (text or "").strip():
            continue
        try:
            result = await _call_json(
                [{"role": "system", "content": JOURNALIST_TG_SYSTEM},
                 {"role": "user", "content": JOURNALIST_TG_USER.format(text=(text or "")[:4000])}],
                model, providers, 300, HeadlineTitleResult)
        except Exception as exc:
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
        log.warning("journalist: бюджет исчерпан (%.2f/%.2f)", spent, budget)
        return
    
    web_n = await _fetch_web()
    tg_n = await _fetch_tg()
    log.info("journalist: добавлено заголовков web=%d tg=%d", web_n, tg_n)