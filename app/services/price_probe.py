"""Авто-прайс-бук: фактические цены пар (модель, провайдер-пин) по ценам эндпоинтов.

Цена эндпоинта закреплённого провайдера — биллинговая правда: каталог слага
даёт смешанную цену (расхождение до 1.67x измерено), а эндпоинт — ту, по которой
тарифицируется пин. Освежение бесплатное (GET /models/.../endpoints), без лага
и без пробных вызовов: цикл price_watch обновляет model_price_book, openrouter.cost
берёт цены оттуда, гарды и учёт становятся биллингово-точными.

Метод дельты /api/v1/credits retired: биллинг обновляется с лагом, дельта вокруг
вызова за секунды равна нулю. Строки source='manual' авто-освежение НЕ трогает:
верифицированные владельцем цены неприкасаемы.

Формат эндпоинта (диагностика 20.10): цены плоские на верхнем уровне
(prompt, completion — за токен), провайдер+квантование в полях provider/
quantization/key; матчинг пина — штатным price_watch._choose_endpoints.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings as cfg
from app.db.models import ModelPriceBook
from app.db.session import session_scope
from app.services.llm_pipeline import _parse_model_spec, _split_model_list
from app.services.price_watch import (POOLED, _choose_endpoints, _fetch_endpoints,
                                      _label, _watched_targets)
from app.services.settings import Keys, get_setting

log = logging.getLogger("price_probe")


async def _upsert(slug: str, provider: str | None, p_in: float, p_out: float,
                  cache_read: float | None, source: str) -> None:
    async with session_scope() as session:
        row = (await session.execute(
            select(ModelPriceBook).where(ModelPriceBook.model_slug == slug,
                                         ModelPriceBook.provider == provider)
        )).scalar_one_or_none()
        # иммунитет manual-строк снят 31.10: эндпоинт-цены = биллинговая правда,
        # сиды были затравкой; дрейф скидок/наценок освежается автоматически
        if row is None:
            row = ModelPriceBook(model_slug=slug, provider=provider)
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
            # голый пин избыточен, есть ли пин с квантованием того же провайдера:
            # иначе две строки книги схлопнутся в один нормализованный ключ
            providers = {p for p in providers
                         if not any(q != p and q.startswith(p + "/") for q in providers)}
            if not providers:
                continue   # без пина правда = каталог, книгу не трогаем
            try:
                eps = await _fetch_endpoints(slug, key)
            except Exception as exc:  # noqa: BLE001
                log.warning("price_probe: %s эндпоинты недоступны (%s: %s)",
                            slug, exc.__class__.__name__, exc)
                continue
            if not eps:
                log.warning("price_probe: %s список эндпоинтов пуст — права ключа "
                            "или недоступность API; пары пропущены", slug)
                continue
            for p in sorted(providers):
                chosen = _choose_endpoints(eps, "pinned", {p})
                if not chosen:
                    log.warning("price_probe: пара %s (%s) — эндпоинт не найден",
                                slug, p)
                    continue
                e = chosen[0]
                try:
                    p_in = float(e.get("prompt") or 0)       # плоские поля эндпоинта:
                    p_out = float(e.get("completion") or 0)  # цены за токен на верхнем уровне
                except (TypeError, ValueError):
                    p_in = p_out = 0.0
                if p_in <= 0 or p_out <= 0:
                    log.warning("price_probe: пара %s (%s) — эндпоинт без цены",
                                slug, p)
                    continue
                await _upsert(slug, p, p_in, p_out, None, "endpoint")
                log.info("price_probe: %s (%s) in=$%.3g/1M out=$%.3g/1M (endpoint %s)",
                         slug, p, p_in * 1e6, p_out * 1e6, _label(e))
                n += 1
    return n