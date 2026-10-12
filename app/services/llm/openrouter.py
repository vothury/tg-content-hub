"""Прямой клиент OpenRouter.

- чат-комплишены с ретраем на транзитные ошибки;
- токены из ответа (usage), включая кешированные (prompt caching);
- стоимость: сначала ручной прайс-бук model_price_book (фактические цены пар
  модель+провайдер, source manual/probe), затем каталог /models как запасной слой;
  кешированные токены считаются по цене cache_read, если провайдер её публикует;
- session_id для sticky routing (аффинность провайдера ради prompt-кэша);
- логирование вызовов в llm_calls выполняет вызывающий код.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 524}


class OpenRouterError(Exception):
    """Сетевая ошибка, ошибка API или неожиданный формат ответа."""


@dataclass
class LLMResponse:
    content: str
    model: str
    provider: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    latency_ms: int
    finish_reason: str | None
    cached_tokens: int = 0   # prompt caching: сколько входных токенов отдано из кэша
    price_source: str = "catalog"   # manual/probe (пара прайс-бука) | catalog | free


class PriceBook:
    """Кэш цен моделей из /models: цена за токен (вход, выход, cache_read).

    Поверх каталога — ручной прайс-бук model_price_book (пары модель+провайдер):
    пины провайдеров могут тарифицироваться иначе каталога слага."""

    def __init__(self) -> None:
        self._prices: dict[str, tuple[float, float, float | None]] = {}
        self._overrides: dict[tuple[str, str | None],
                              tuple[float, float, float | None, str]] = {}
        self._loaded = False

    async def load(self, client: httpx.AsyncClient) -> None:
        if self._loaded:
            return
        try:
            resp = await client.get(f"{settings.openrouter_base_url}/models", timeout=30)
            resp.raise_for_status()
            for item in resp.json().get("data", []):
                pricing = item.get("pricing") or {}
                try:
                    prompt_price = float(pricing.get("prompt", 0))
                    completion_price = float(pricing.get("completion", 0))
                except (TypeError, ValueError):
                    continue
                cache_read: float | None = None
                try:
                    cache_read = (float(pricing["cache_read"])
                                  if pricing.get("cache_read") else None)
                except (TypeError, ValueError):
                    cache_read = None
                self._prices[item.get("id", "")] = (prompt_price, completion_price,
                                                    cache_read)
            self._loaded = True
            log.info("прайс-кэш загружен: %d моделей", len(self._prices))
        except Exception as exc:  # noqa: BLE001 — цены некритичны
            log.warning("не удалось загрузить прайс-кэш: %s", exc)
        if self._loaded:
            await self._load_overrides()

    async def _load_overrides(self) -> None:
        try:
            from sqlalchemy import select

            from app.db.models import ModelPrice
            from app.db.session import session_scope
            async with session_scope() as session:
                rows = (await session.execute(select(ModelPrice))).scalars().all()
            self._overrides = {}
            for r in rows:
                val = (r.price_in_per_token, r.price_out_per_token,
                       r.cache_read_per_token, r.source)
                keys = {(r.model_slug, r.provider)}
                if r.provider:
                    p = r.provider.strip().lower()
                    keys.add((r.model_slug, p))
                    keys.add((r.model_slug, p.split("/")[0]))
                for k in keys:
                    self._overrides[k] = val
            log.info("прайс-бук: %d ручных цен пар", len(self._overrides))
        except Exception as exc:  # noqa: BLE001 — прайс-бук некритичен
            log.warning("прайс-бук: не удалось загрузить ручные цены: %s", exc)

    def cost(self, model: str, input_tokens: int | None, output_tokens: int | None,
             cached_tokens: int = 0,
             provider: str | None = None) -> tuple[float | None, str]:
        ov = None
        if provider:
            s = str(provider).strip().lower()
            for cand in (provider, s, s.split("/")[0], s.split()[0] if s else s):
                ov = self._overrides.get((model, cand))
                if ov is not None:
                    break
        if ov is None:
            ov = self._overrides.get((model, None))
        if ov is not None:
            prices: tuple[float, float, float | None] = ov[:3]
            source = ov[3]
        else:
            prices = self._prices.get(model) or None
            source = "catalog"
        if not prices or input_tokens is None or output_tokens is None:
            return None, source
        prompt_price, completion_price, cache_read = prices
        total = input_tokens * prompt_price + output_tokens * completion_price
        if cached_tokens and cache_read is not None:
            # провайдер публикует цену cache_read: кешированные токены считаем по ней,
            # вычитая их долю из полной стоимости входа
            total -= cached_tokens * prompt_price
            total += cached_tokens * cache_read
        return total, source


_price_book = PriceBook()


async def chat_completion(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float = 0.4,
    provider: dict | None = None,
    reasoning_max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    session_id: str | None = None,
) -> LLMResponse:
    """Вызов чат-комплишена с одной повторной попыткой. Бросает OpenRouterError."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # Бюджет внутренних рассуждений: защищает от зацикливания reasoning-моделей.
    # effort имеет приоритет над max_tokens: "none" выключает рассуждения целиком
    # (у моделей с поддержкой effort), остальные уровни - относительный бюджет.
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    elif reasoning_max_tokens:
        payload["reasoning"] = {"max_tokens": reasoning_max_tokens}
    # Предпочтения провайдеров передаются как есть; пусто = авто-маршрутизация
    if provider:
        payload["provider"] = provider
    # Sticky routing: держим запросы на одном провайдере, чтобы жил prompt-кэш
    if session_id:
        payload["session_id"] = session_id
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    started = time.monotonic()
    last_error: Exception | None = None
    data: dict | None = None

    async with httpx.AsyncClient(timeout=settings.openrouter_request_timeout_sec) as client:
        await _price_book.load(client)
        for attempt in (1, 2):
            try:
                resp = await client.post(
                    f"{settings.openrouter_base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt == 1:
                    await asyncio.sleep(3)
                    continue
                raise OpenRouterError(f"сетевая ошибка: {exc}") from exc

            if resp.status_code in RETRYABLE_STATUS and attempt == 1:
                try:
                    retry_after = int(resp.headers.get("Retry-After", "3") or 3)
                except ValueError:
                    retry_after = 3
                delay = min(retry_after, 30)
                log.warning("OpenRouter HTTP %s — повтор через %d сек",
                            resp.status_code, delay)
                await asyncio.sleep(delay)
                continue
            if resp.status_code != 200:
                raise OpenRouterError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            break

    if data is None:
        raise OpenRouterError(f"запрос не выполнен: {last_error}")

    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(
            f"неожиданная структура ответа: {json.dumps(data)[:500]}") from exc

    usage = data.get("usage") or {}
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    cached_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
    answer_model = data.get("model", model)
    free = str(answer_model).endswith(":free")
    cost_usd, price_source = (
        (0.0, "free") if free else _price_book.cost(
            answer_model, input_tokens, output_tokens, cached_tokens,
            data.get("provider")))
    return LLMResponse(
        content=content,
        model=answer_model,
        provider=data.get("provider"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        price_source=price_source,
        latency_ms=latency_ms,
        finish_reason=finish_reason,
        cached_tokens=cached_tokens,
    )


async def credits_usage() -> float | None:
    """Накопленная трата (total_usage) из /api/v1/credits — основа биллинговой правды."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{settings.openrouter_base_url}/credits",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"})
            resp.raise_for_status()
            return float(resp.json()["data"]["total_usage"])
    except Exception as exc:  # noqa: BLE001 — кредиты некритичны
        log.warning("credits недоступны: %s", exc)
        return None