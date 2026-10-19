"""Авто-прайс-бук: фактические цены пар (модель, провайдер-пин) по ценам эндпоинтов.

Цена эндпоинта закреплённого провайдера — биллинговая правда: каталог слага
даёт смешанную цену (расхождение до 1.67x измерено), а эндпоинт — ту, по которой
тарифицируется пин. Освежение бесплатное (GET /models/.../endpoints), без лага
и без пробных вызовов: цикл price_watch обновляет model_price_book, openrouter.cost
берёт цены оттуда, гарды и учёт становятся биллингово-точными.

Метод дельты /api/v1/credits retired: биллинг обновляется с лагом, дельта вокруг
вызова за секунды равна нулю. Строки source='manual' авто-освежение НЕ трогает:
верифицированные владельцем цены неприкасаемы.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings as cfg
from app.db.models import ModelPrice
from app.db.session import session_scope
from app.services.llm_pipeline import _parse_model_spec, _split_model_list
from app.services.price_watch import (POOLED, _fetch_endpoints, _watched_targets)
from app.services.settings import Keys, get_setting

log = logging.getLogger("price_probe")


def _match_endpoint(eps: list, pin: str) -> dict | None:
    """Эндпоинт, соответствующий пину: провайдер + квантование/тир в метке."""
    prov, _, tag = pin.partition("/")
    prov = prov.strip().lower()
    tag = tag.strip().lower()
    for e in eps or []:
        name = str(e.get("provider_name") or e.get("name") or "").lower()
        quant = " ".join(str(q).lower() for q in (e.get("quantizations") or []))
        label = str(e.get("name") or "").lower()
        if prov and prov not in name and prov not in label:
            continue
        if tag and tag not in quant and tag not in label:
            continue
        pr = e.get("pricing") or {}
        try:
            p_in = float(pr.get("prompt") or 0)
            p_out = float(pr.get("completion") or 0)
        except (TypeError, ValueError):
            continue
        if p_in <= 0 or p_out <= 0:
            continue
        return e
    return None


async def _upsert(slug: str, provider: str | None, p_in: float, p_out: float,
                  cache_read: float | None, source: str) -> None:
    async with session_scope() as session:
        row = (await session.execute(
            select(ModelPrice).where(ModelPrice.model_slug == slug,
                                     ModelPrice.provider == provider)
        )).scalar_one_or_none()
        if row is not None and row.source == "manual":
            return   # верифицированная владельцем цена неприкасаема
        if row is None:
            row = ModelPrice(model_slug=slug, provider=provider)
            session.add(row)
        row.price_in_per_token = p_in
        row.price_out_per_token = p_out
        row.cache_read_per_token = cache_read
        row.source = source
        row.note = "endpoint pricing auto-refresh"
        row.verified_at = datetime.now(timezone.utc)
        await session.commit()


async def probe_cycle() -> int:
    """Освежает книгу цен по эндпоинтам всех пинов из настроек стадий."""
    targets = await _watched_targets()
    async with session_scope() as session:
        mgmt = str(await get_setting(session, Keys.OPENROUTER_MANAGEMENT_KEY) or "").strip()
    key = mgmt or cfg.openrouter_api_key
    n = 0
    for raw_model, pinned in targets.items():
        for spec in _split_model_list(str(raw_model)):
            slug, prov = _parse_model_spec(spec)
            if not slug or slug in POOLED or "/free" in slug or slug.endswith(":free"):
                continue   # пулы и free не тарифицируются — в книге им делать нечего
            providers = set(pinned or ())
            if isinstance(prov, dict):
                providers.update(str(x) for x in (prov.get("order") or []))
            providers = {p for p in providers if p and "/free" not in p}
            if not providers:
                continue   # без пина правда = каталог, книгу не трогаем
            try:
                eps = await _fetch_endpoints(slug, key)
            except Exception as exc:  # noqa: BLE001
                log.warning("price_probe: %s эндпоинты недоступны (%s: %s)",
                            slug, exc.__class__.__name__, exc)
                continue
            for p in sorted(providers):
                e = _match_endpoint(eps, p)
                if e is None:
                    log.warning("price_probe: пара %s (%s) — эндпоинт не найден",
                                slug, p)
                    continue
                pr = e.get("pricing") or {}
                p_in = float(pr.get("prompt") or 0)
                p_out = float(pr.get("completion") or 0)
                try:
                    cache_read = float(pr["cache_read"]) if pr.get("cache_read") else None
                except (TypeError, ValueError):
                    cache_read = None
                await _upsert(slug, p, p_in, p_out, cache_read, "endpoint")
                log.info("price_probe: %s (%s) in=$%.3g/1M out=$%.3g/1M (endpoint)",
                         slug, p, p_in * 1e6, p_out * 1e6)
                n += 1
    return n