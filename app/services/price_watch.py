"""Контроль цен моделей OpenRouter.

Раз в interval_hours снимает цены из открытого каталога моделей, хранит историю
изменений и создаёт предупреждение, если цена используемой модели изменилась
сильнее порога. Расхода токенов нет: только один HTTP-запрос каталога.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.db.models import LLMCall, ModelPrice, ModelPriceAlert
from app.db.session import session_scope
from app.services.settings import Keys, get_setting

log = logging.getLogger("price_watch")

MODELS_URL = "https://openrouter.ai/api/v1/models"   # публичный каталог моделей с ценами, без ключа
POOLED = {"openrouter/free", "openrouter/auto"}
MODEL_KEYS = (
    "CLASSIFY_MODEL", "REWRITE_MODEL", "REVISION_MODEL", "PREFILTER_MODEL",
    "DOUBLE_CHECK_MODEL", "DOUBLE_CHECK_ONLINE_MODEL", "EDITORIAL_CHIEF_MODEL",
    "EDITORIAL_JOURNALIST_MODEL", "EDITORIAL_BROWSE_MODEL",
    "CLEAN_FALLBACK_MODEL", "LLM_FALLBACK_MODELS",
)


def _norm(model: str) -> str:
    """~slug:online -> slug (суффикс плагина и маркер роутинга не влияют на цену)."""
    m = (model or "").strip().lstrip("~")
    return re.sub(r":online$", "", m)


def _pct(old: float, new: float) -> float:
    if not old:
        return 0.0
    return round((new - old) / old * 100.0, 1)


async def _watched_models() -> set[str]:
    """Модели из настроек + те, что реально вызывались за последние 7 дней."""
    models: set[str] = set()
    async with session_scope() as session:
        for name in MODEL_KEYS:
            key = getattr(Keys, name, None)
            if key is None:
                continue
            try:
                v = await get_setting(session, key)
            except Exception:  # noqa: BLE001 — ключ мог ещё не появиться
                continue
            if isinstance(v, list):
                models.update(_norm(str(x)) for x in v if str(x).strip())
            elif v:
                models.add(_norm(str(v)))
        since = datetime.now(timezone.utc) - timedelta(days=7)
        rows = (await session.execute(
            select(LLMCall.model).where(LLMCall.created_at >= since,
                                        LLMCall.model.isnot(None)).distinct())).scalars().all()
        models.update(_norm(r) for r in rows if r)
    return {m for m in models if m and "/" in m and m not in POOLED}


async def _fetch_pricing() -> dict[str, dict]:
    """Один бесплатный запрос каталога OpenRouter (с одной повторной попыткой)."""
    last_error = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(
                    timeout=45, follow_redirects=True,
                    headers={"User-Agent": "TGContentHub/1.0"}) as client:
                r = await client.get(MODELS_URL)
            r.raise_for_status()
            data = r.json().get("data") or []
            out: dict[str, dict] = {}
            for m in data:
                mid = str(m.get("id") or "").strip()
                pr = m.get("pricing") or {}
                if not mid:
                    continue
                try:
                    out[mid] = {
                        "prompt": float(pr.get("prompt") or 0),
                        "completion": float(pr.get("completion") or 0),
                        "request": float(pr.get("request") or 0),
                    }
                except (TypeError, ValueError):
                    continue
            log.info("price_watch: каталог получен — %d моделей", len(out))
            return out
        except Exception as exc:  # noqa: BLE001
            last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning("price_watch: попытка %d, %s -> %s", attempt, MODELS_URL, last_error)
            if attempt == 1:
                await asyncio.sleep(10)
    raise RuntimeError(f"каталог OpenRouter недоступен: {last_error}")


async def watch_models() -> int:
    """Один проход сверки. Возвращает число созданных предупреждений."""
    async with session_scope() as session:
        enabled = int(await get_setting(session, Keys.PRICE_WATCH_ENABLED))
        threshold = float(await get_setting(session, Keys.PRICE_ALERT_PCT))
    if not enabled:
        return 0
    watched = await _watched_models()
    if not watched:
        return 0
    try:
        pricing = await _fetch_pricing()
    except Exception as exc:  # noqa: BLE001
        log.warning("price_watch: каталог OpenRouter недоступен (%s: %s) — url: %s",
                    exc.__class__.__name__, exc, MODELS_URL)
        return 0
    now = datetime.now(timezone.utc)
    created = 0
    async with session_scope() as session:
        for model in sorted(watched):
            cur = pricing.get(model)
            if cur is None:
                log.info("price_watch: %s отсутствует в каталоге OpenRouter", model)
                continue
            prev = (await session.execute(
                select(ModelPrice).where(ModelPrice.model == model)
                .order_by(ModelPrice.id.desc()).limit(1))).scalar_one_or_none()
            unchanged = (prev is not None
                         and abs(prev.prompt_usd - cur["prompt"]) < 1e-12
                         and abs(prev.completion_usd - cur["completion"]) < 1e-12)
            if prev is None or not unchanged:
                session.add(ModelPrice(model=model, prompt_usd=cur["prompt"],
                                       completion_usd=cur["completion"],
                                       request_usd=cur["request"], fetched_at=now))
            if prev is None or unchanged:
                continue
            pct_p = _pct(prev.prompt_usd or 0.0, cur["prompt"])
            pct_c = _pct(prev.completion_usd or 0.0, cur["completion"])
            worst = max(pct_p, pct_c)
            if abs(worst) < threshold:
                continue
            exists = (await session.execute(
                select(ModelPriceAlert.id).where(
                    ModelPriceAlert.model == model,
                    ModelPriceAlert.acknowledged.is_(False),
                    ModelPriceAlert.created_at >= now - timedelta(days=14))
                .limit(1))).scalar_one_or_none()
            if exists is not None:
                continue
            session.add(ModelPriceAlert(
                model=model, old_prompt=prev.prompt_usd, new_prompt=cur["prompt"],
                old_completion=prev.completion_usd, new_completion=cur["completion"],
                change_pct=abs(worst), direction="up" if worst > 0 else "down"))
            created += 1
            log.warning("price_watch: %s цена %s: input $%.4f -> $%.4f (%.1f%%), "
                        "output $%.4f -> $%.4f",
                        model, "выросла" if worst > 0 else "снизилась",
                        prev.prompt_usd or 0, cur["prompt"], pct_p,
                        prev.completion_usd or 0, cur["completion"], pct_c)
        await session.commit()
    log.info("price_watch: проверено моделей %d, новых предупреждений %d", len(watched), created)
    return created


async def active_alerts(limit: int = 5) -> list[dict]:
    async with session_scope() as session:
        rows = (await session.execute(
            select(ModelPriceAlert).where(ModelPriceAlert.acknowledged.is_(False))
            .order_by(ModelPriceAlert.id.desc()).limit(limit))).scalars().all()
        return [{
            "id": a.id, "model": a.model, "direction": a.direction,
            "change_pct": float(a.change_pct or 0),
            "old_prompt": float(a.old_prompt or 0), "new_prompt": float(a.new_prompt or 0),
            "old_completion": float(a.old_completion or 0),
            "new_completion": float(a.new_completion or 0),
        } for a in rows]


async def acknowledge(ids: list[int]) -> int:
    if not ids:
        return 0
    async with session_scope() as session:
        rows = (await session.execute(
            select(ModelPriceAlert).where(ModelPriceAlert.id.in_(ids)))).scalars().all()
        n = 0
        for a in rows:
            if not a.acknowledged:
                a.acknowledged = True
                n += 1
        await session.commit()
    return n


async def loop() -> None:
    """Фоновый цикл в api-контейнере: проверка раз в interval_hours."""
    while True:
        try:
            await watch_models()
        except Exception:  # noqa: BLE001
            log.exception("price_watch: сбой проверки")
        hours = 24
        try:
            async with session_scope() as session:
                hours = int(await get_setting(session, Keys.PRICE_WATCH_INTERVAL_HOURS))
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(max(1, hours) * 3600)