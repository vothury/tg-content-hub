"""Контроль цен моделей OpenRouter: по модели И по провайдерам.

Цена в общем каталоге (/api/v1/models) — это цена ТОПОВОГО провайдера модели,
а фактическая стоимость зависит от выбранного при роутинге эндпоинта. Поэтому
дополнительно опрашиваем /api/v1/models/{author}/{slug}/endpoints и ведём историю
цен по каждому интересующему провайдеру (режим price_watch_scope).

Расхода токенов нет: только HTTP-запросы каталога (раз в interval_hours).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import settings
from app.db.models import LLMCall, ModelPrice, ModelPriceAlert
from app.db.session import session_scope
from app.redis_client import get_redis
from app.services.settings import Keys, get_providers, get_setting

log = logging.getLogger("price_watch")

MODELS_URL = "https://openrouter.ai/api/v1/models"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{author}/{slug}/endpoints"
POOLED = {"openrouter/free", "openrouter/auto"}

# пары «модель стадии -> закреплённые провайдеры стадии»
STAGE_PAIRS = (
    ("CLASSIFY_MODEL", "CLASSIFY_PROVIDERS"),
    ("REWRITE_MODEL", "REWRITE_PROVIDERS"),
    ("REVISION_MODEL", "REVISION_PROVIDERS"),
    ("PREFILTER_MODEL", "PREFILTER_PROVIDERS"),
    ("DOUBLE_CHECK_MODEL", "DOUBLE_CHECK_PROVIDERS"),
    ("DOUBLE_CHECK_ONLINE_MODEL", "DOUBLE_CHECK_ONLINE_PROVIDERS"),
    ("EDITORIAL_CHIEF_MODEL", "EDITORIAL_CHIEF_PROVIDERS"),
    ("EDITORIAL_JOURNALIST_MODEL", "EDITORIAL_JOURNALIST_PROVIDERS"),
)
EXTRA_MODEL_KEYS = ("CLEAN_FALLBACK_MODEL", "LLM_FALLBACK_MODELS", "DEDUP_CONFIRM_MODEL")


def _norm(model: str) -> str:
    """~slug:online -> slug (маркер роутинга и суффикс плагина на цену не влияют)."""
    return re.sub(r":online$", "", (model or "").strip().lstrip("~"))


def _valid(model: str) -> bool:
    return bool(model) and "/" in model and model not in POOLED


def _pct(old: float, new: float) -> float:
    if not old:
        return 0.0 if not new else 100.0   # платная опция появилась там, где было 0
    return round((new - old) / old * 100.0, 1)


def _variants(model: str) -> list[str]:
    """Варианты slug'а: точный, без :варианта, без -latest, :free."""
    base = model.split(":")[0]
    out = [model, base]
    if base.endswith("-latest"):
        out.append(base[: -len("-latest")])
        out.append(base[: -len("-latest")] + ":free")
    out.append(base + ":free")
    seen, res = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            res.append(v)
    return res


def _price_dict(pr: dict) -> dict | None:
    try:
        return {
            "prompt": float(pr.get("prompt") or 0),
            "completion": float(pr.get("completion") or 0),
            "request": float(pr.get("request") or 0),
            "web_search": float(pr.get("web_search") or 0),
            "overrides": bool(pr.get("overrides")),
        }
    except (TypeError, ValueError):
        return None


async def _watched_targets() -> dict:
    """{модель: {закреплённые провайдеры}} — из настроек стадий и из llm_calls за 7 дней."""
    targets: dict = {}
    async with session_scope() as session:
        for mname, pname in STAGE_PAIRS:
            mk, pk = getattr(Keys, mname, None), getattr(Keys, pname, None)
            if mk is None:
                continue
            try:
                mv = await get_setting(session, mk)
            except Exception:  # noqa: BLE001
                continue
            models = [_norm(str(x)) for x in mv] if isinstance(mv, list) \
                else ([_norm(str(mv))] if mv else [])
            provs: set = set()
            if pk is not None:
                try:
                    pv = await get_providers(session, pk)
                except Exception:  # noqa: BLE001
                    pv = None
                if isinstance(pv, str):
                    s = pv.strip()
                    try:
                        pv = (json.loads(s) if s[:1] in ("{", "[")
                              else [x.strip() for x in s.split(",") if x.strip()])
                    except Exception:  # noqa: BLE001
                        pv = [x.strip() for x in s.split(",") if x.strip()]
                if isinstance(pv, dict):
                    for field in ("order", "only"):
                        v = pv.get(field)
                        if isinstance(v, list):
                            provs.update(str(x).strip() for x in v if str(x).strip())
                        elif isinstance(v, str) and v.strip():
                            provs.add(v.strip())
                elif isinstance(pv, list):
                    provs.update(str(x).strip() for x in pv if str(x).strip())
            for m in models:
                if _valid(m):
                    targets.setdefault(m, set()).update(provs)
        for name in EXTRA_MODEL_KEYS:
            k = getattr(Keys, name, None)
            if k is None:
                continue
            try:
                v = await get_setting(session, k)
            except Exception:  # noqa: BLE001
                continue
            for x in (v if isinstance(v, list) else ([v] if v else [])):
                m = _norm(str(x))
                if _valid(m):
                    targets.setdefault(m, set())
        since = datetime.now(timezone.utc) - timedelta(days=7)
        rows = (await session.execute(
            select(LLMCall.model).where(LLMCall.created_at >= since,
                                        LLMCall.model.isnot(None)).distinct())).scalars().all()
        for r in rows:
            m = _norm(str(r))
            if _valid(m):
                targets.setdefault(m, set())
    return targets


async def _watched_models() -> list[str]:
    return sorted(await _watched_targets())


async def _fetch_pricing() -> dict:
    """Общий каталог: агрегированная цена модели (по факту — цена топ-провайдера)."""
    last_error = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(
                    timeout=45, follow_redirects=True,
                    headers={"User-Agent": "TGContentHub/1.0"}) as client:
                r = await client.get(MODELS_URL)
            r.raise_for_status()
            out: dict = {}
            for m in (r.json().get("data") or []):
                mid = str(m.get("id") or "").strip()
                if not mid:
                    continue
                p = _price_dict(m.get("pricing") or {})
                if p is not None:
                    out[mid] = p
            log.info("price_watch: каталог получен — %d моделей", len(out))
            return out
        except Exception as exc:  # noqa: BLE001
            last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning("price_watch: попытка %d, %s -> %s", attempt, MODELS_URL, last_error)
            if attempt == 1:
                await asyncio.sleep(10)
    raise RuntimeError(f"каталог OpenRouter недоступен: {last_error}")


async def _fetch_endpoints(model: str, key: str) -> list:
    """Цены по провайдерам модели. Пробуем все варианты slug'а (алиасы/-latest/:free).

    Пустой список — если недоступно (403 без management-ключа, 404, сеть).
    """
    headers = {"User-Agent": "TGContentHub/1.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    status, tried = None, []
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers=headers) as client:
            for variant in _variants(model):
                author, _, slug = variant.partition("/")
                if not author or not slug:
                    continue
                tried.append(variant)
                r = await client.get(ENDPOINTS_URL.format(author=author, slug=slug))
                status = r.status_code
                if status != 200:
                    continue
                eps = ((r.json() or {}).get("data") or {}).get("endpoints") or []
                out = []
                for e in eps:
                    p = _price_dict(e.get("pricing") or {})
                    if p is None:
                        continue
                    p["provider"] = str(e.get("provider_name") or e.get("tag") or "?")
                    p["key"] = str(e.get("tag") or e.get("provider_name") or "?") \
                        .strip().lower().replace(" ", "-")
                    p["name"] = str(e.get("name") or "").strip()
                    p["quantization"] = str(e.get("quantization") or "")
                    p["context_length"] = e.get("context_length")
                    p["uptime"] = e.get("uptime_last_30m")
                    out.append(p)
                return out
    except Exception as exc:  # noqa: BLE001
        log.warning("price_watch: endpoints %s -> %s: %s", model, exc.__class__.__name__, exc)
        return []
    hint = " — нужен management-ключ (OPENROUTER_MANAGEMENT_KEY)" if status == 403 else ""
    log.warning("price_watch: endpoints для %s недоступны (HTTP %s; пробовал: %s)%s",
                model, status, ", ".join(tried), hint)
    return []


def _cheapest(eps: list) -> list:
    return [min(eps, key=lambda e: e["prompt"] + e["completion"])] if eps else []


def _choose_endpoints(eps: list, scope: str, pinned: set) -> list:
    """Отбор точек наблюдения: все / закреплённые в настройках / самая дешёвая."""
    if not eps:
        return []
    if scope == "all":
        return eps
    if scope == "pinned" and pinned:
        norm = {str(p).strip().lower() for p in pinned}
        chosen = [e for e in eps
                  if e.get("key") in norm
                  or str(e.get("provider", "")).strip().lower().replace(" ", "-") in norm]
        if chosen:
            return chosen
        log.info("price_watch: закреплённые провайдеры %s среди эндпоинтов не найдены — "
                 "беру самый дешёвый", sorted(norm))
    return _cheapest(eps)


def _label(e: dict) -> str:
    """Уникальная человекочитаемая подпись эндпоинта (провайдеры повторяются с разной ценой)."""
    base = str(e.get("name") or e.get("provider") or "?")
    parts = []
    q = (e.get("quantization") or "").strip().lower()
    if q and q != "unknown":
        parts.append(q)
    ctx = e.get("context_length")
    if ctx:
        parts.append(f"{int(ctx) // 1000}k")
    return base + (" (" + ", ".join(parts) + ")" if parts else "")


async def _last_price(session, model: str, provider: str):
    return (await session.execute(
        select(ModelPrice).where(ModelPrice.model == model, ModelPrice.provider == provider)
        .order_by(ModelPrice.id.desc()).limit(1))).scalar_one_or_none()


async def _track(session, model: str, provider: str, cur: dict, prev, now,
                 threshold: float, history_min_pct: float) -> int:
    """Сверяет одну точку наблюдения; пишет историю и, при необходимости, алерт."""
    if prev is None:
        session.add(ModelPrice(model=model, provider=provider, prompt_usd=cur["prompt"],
                               completion_usd=cur["completion"],
                               request_usd=cur.get("request", 0.0),
                               web_search_usd=cur.get("web_search", 0.0), fetched_at=now))
        return 0
    pct_p = _pct(prev.prompt_usd or 0.0, cur["prompt"])
    pct_c = _pct(prev.completion_usd or 0.0, cur["completion"])
    pct_w = _pct(prev.web_search_usd or 0.0, cur.get("web_search") or 0.0)
    if max(abs(pct_p), abs(pct_c), abs(pct_w)) < history_min_pct:
        return 0
    session.add(ModelPrice(model=model, provider=provider, prompt_usd=cur["prompt"],
                           completion_usd=cur["completion"],
                           request_usd=cur.get("request", 0.0),
                           web_search_usd=cur.get("web_search", 0.0), fetched_at=now))
    worst = max(pct_p, pct_c, pct_w)
    if abs(worst) < threshold:
        return 0
    exists = (await session.execute(
        select(ModelPriceAlert.id).where(
            ModelPriceAlert.model == model, ModelPriceAlert.provider == provider,
            ModelPriceAlert.acknowledged.is_(False),
            ModelPriceAlert.created_at >= now - timedelta(days=14)).limit(1))).scalar_one_or_none()
    if exists is not None:
        return 0
    session.add(ModelPriceAlert(
        model=model, provider=provider, old_prompt=prev.prompt_usd, new_prompt=cur["prompt"],
        old_completion=prev.completion_usd, new_completion=cur["completion"],
        change_pct=abs(worst), direction="up" if worst > 0 else "down"))
    log.warning("price_watch: %s [%s] цена %s: input $%.3f -> $%.3f за 1M (%.1f%%), "
                "output $%.3f -> $%.3f за 1M (%.1f%%)",
                model, provider or "агрегат", "выросла" if worst > 0 else "снизилась",
                (prev.prompt_usd or 0) * 1e6, cur["prompt"] * 1e6, pct_p,
                (prev.completion_usd or 0) * 1e6, cur["completion"] * 1e6, pct_c)
    return 1


async def watch_models() -> int:
    """Один проход сверки. Возвращает число созданных предупреждений."""
    async with session_scope() as session:
        enabled = int(await get_setting(session, Keys.PRICE_WATCH_ENABLED))
        threshold = float(await get_setting(session, Keys.PRICE_ALERT_PCT))
        history_min_pct = float(await get_setting(session, Keys.PRICE_HISTORY_MIN_CHANGE_PCT))
        scope = str(await get_setting(session, Keys.PRICE_WATCH_SCOPE)).strip().lower()
        mgmt_key = str(await get_setting(session, Keys.OPENROUTER_MANAGEMENT_KEY) or "").strip()
    if not enabled:
        return 0
    targets = await _watched_targets()
    if not targets:
        return 0
    try:
        pricing = await _fetch_pricing()
    except Exception as exc:  # noqa: BLE001
        log.warning("price_watch: каталог OpenRouter недоступен (%s: %s) — url: %s",
                    exc.__class__.__name__, exc, MODELS_URL)
        return 0

    key = mgmt_key or settings.openrouter_api_key
    points: list = []          # (model, provider_label, price_dict)
    for model in sorted(targets):
        cur, matched = None, model
        for v in _variants(model):
            if v in pricing:
                cur, matched = pricing[v], v
                break
        if cur is None:
            log.warning("price_watch: %s не найден в каталоге (пробовал: %s) — проверьте slug",
                        model, ", ".join(_variants(model)))
            continue
        if matched != model:
            log.info("price_watch: %s найден в каталоге как %s", model, matched)
        points.append((model, "", cur))
        if scope in ("pinned", "cheapest", "all"):
            eps = await _fetch_endpoints(model, key)
            for e in _choose_endpoints(eps, scope, targets.get(model) or set()):
                if cur.get("web_search") or e.get("web_search"):
                    log.info("price_watch: %s [%s] — web_search $%.4f за вызов",
                             model, _label(e), e.get("web_search") or 0.0)
                points.append((model, _label(e), e))

    now = datetime.now(timezone.utc)
    created = 0
    async with session_scope() as session:
        for model, provider, cur in points:
            prev = await _last_price(session, model, provider)
            created += await _track(session, model, provider, cur, prev, now,
                                    threshold, history_min_pct)
        await session.commit()
    log.info("price_watch: точек наблюдения %d (моделей %d, режим %s), новых предупреждений %d "
             "(порог истории %.1f%%, порог предупреждения %.1f%%)",
             len(points), len(targets), scope or "off", created, history_min_pct, threshold)
    return created


async def active_alerts(limit: int = 5) -> list[dict]:
    async with session_scope() as session:
        rows = (await session.execute(
            select(ModelPriceAlert).where(ModelPriceAlert.acknowledged.is_(False))
            .order_by(ModelPriceAlert.id.desc()).limit(limit))).scalars().all()
        return [{
            "id": a.id, "model": a.model, "provider": a.provider or "",
            "direction": a.direction, "change_pct": float(a.change_pct or 0),
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
    """Фоновый цикл в api: проверка раз в interval_hours + отметка живости в redis."""
    while True:
        created, error = 0, None
        try:
            created = await watch_models()
        except Exception as exc:  # noqa: BLE001
            error = f"{exc.__class__.__name__}: {exc}"
            log.exception("price_watch: сбой проверки")
        hours = 24
        try:
            async with session_scope() as session:
                hours = int(await get_setting(session, Keys.PRICE_WATCH_INTERVAL_HOURS))
        except Exception:  # noqa: BLE001
            pass
        try:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            await get_redis().set("price_watch:last_run",
                                  f"{stamp} alerts={created}" + (f" error={error}" if error else ""))
        except Exception:  # noqa: BLE001
            pass
        log.info("price_watch: следующая проверка через %d ч", max(1, hours))
        await asyncio.sleep(max(1, hours) * 3600)