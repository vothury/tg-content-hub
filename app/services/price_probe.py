"""Авто-прайс-бук: биллинговая правда дельтами /api/v1/credits вокруг пробных вызовов.

Два пробника на пару (короткий вход и длинный вход, фиксированный малый выход)
дают два уравнения для цен (input, output); дельта берётся минимумом из трёх
попыток — фоновые боевые вызовы только добавляют стоимость, минимум близок к чистой.
Результат пишется в model_price_book (source='probe'); openrouter.cost берёт цену
оттуда, то есть учёт стоимости и гарды бюджетов становятся биллингово-точными
без ручного мониторинга. Ручные сиды — лишь затравка до первого пробника.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import ModelPrice
from app.db.session import session_scope
from app.services.llm.openrouter import chat_completion, credits_usage
from app.services.llm_pipeline import _parse_model_spec, _split_model_list

log = logging.getLogger("price_probe")

_FILLER = ("новостройки Москвы и Подмосковья: цены, спрос, предложение, ипотека, "
           "запуски, ввод, девелоперы, земля, аукционы, реновация, КРТ, ставки, "
           "доступность, сделки, дду, рассрочка, банкротства, сроки, стройка. ")


async def _delta_min(call, attempts: int = 3):
    best = None
    for _ in range(attempts):
        before = await credits_usage()
        if before is None:
            return None
        r = await call()
        after = await credits_usage()
        if after is None:
            return None
        d = after - before
        if d >= 0 and (best is None or d < best[0]):
            best = (d, r.input_tokens or 0, r.output_tokens or 0)
        await asyncio.sleep(1)
    return best


async def probe_pair(slug: str, provider: str | None):
    """Возвращает (цена input за токен, цена output за токен) или None."""
    prov = {"order": [provider], "allow_fallbacks": False} if provider else None
    nonce = secrets.token_hex(4)   # уникальность против prompt-кэша: кэш исказил бы цену

    async def short_call():
        return await chat_completion(
            [{"role": "user", "content": f"ping {nonce}"}], slug, 8, 0.0,
            provider=prov)

    async def long_call():
        return await chat_completion(
            [{"role": "user", "content": _FILLER * 40 + nonce}], slug, 8, 0.0,
            provider=prov)

    a = await _delta_min(short_call)
    b = await _delta_min(long_call)
    if not a or not b:
        return None
    (c1, i1, o1), (c2, i2, o2) = a, b
    if o1 < 1 or o2 < 1 or i2 <= i1 * 3:
        return None
    den = i1 * o2 - i2 * o1
    if den == 0:
        return None
    p_in = (c1 * o2 - c2 * o1) / den
    p_out = (i1 * c2 - i2 * c1) / den
    if not (1e-9 <= p_in <= 5e-5 and 1e-9 <= p_out <= 5e-5):
        log.warning("price_probe: %s (%s) цены вне sanity-полосы: in=%g out=%g",
                    slug, provider, p_in, p_out)
        return None
    return p_in, p_out


async def _upsert(slug: str, provider: str | None, p_in: float, p_out: float) -> None:
    async with session_scope() as session:
        row = (await session.execute(
            select(ModelPrice).where(ModelPrice.model_slug == slug,
                                     ModelPrice.provider == provider)
        )).scalar_one_or_none()
        if row is None:
            row = ModelPrice(model_slug=slug, provider=provider)
            session.add(row)
        row.price_in_per_token = p_in
        row.price_out_per_token = p_out
        row.source = "probe"
        row.note = "auto-probe credits-delta"
        row.verified_at = datetime.now(timezone.utc)
        await session.commit()


async def probe_cycle() -> int:
    """Пробует все пины из настроек стадий; возвращает число обновлённых пар."""
    from app.services.price_watch import _watched_targets
    targets = await _watched_targets()
    n = 0
    for raw_model, pinned in targets.items():
        for spec in _split_model_list(str(raw_model)):
            slug, prov = _parse_model_spec(spec)
            if slug.endswith(":free"):
                continue
            providers = set(pinned or ())
            if isinstance(prov, dict):
                providers.update(str(x) for x in (prov.get("order") or []))
            for p in (sorted(providers) or [None]):
                res = await probe_pair(slug, p)
                if res is None:
                    log.warning("price_probe: пара %s (%s) не пробита "
                                "(credits/дельта недоступны)", slug, p)
                    continue
                p_in, p_out = res
                await _upsert(slug, p, p_in, p_out)
                log.info("price_probe: %s (%s) in=$%.3g/1M out=$%.3g/1M (probe)",
                         slug, p, p_in * 1e6, p_out * 1e6)
                n += 1
                await asyncio.sleep(2)
    return n