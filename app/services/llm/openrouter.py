"""Прямой клиент OpenRouter.

- чат-комплишены с ретраем на транзитные ошибки;
- токены из ответа (usage);
- стоимость по ценам /models (кэш в памяти, цены за токен);
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


class PriceBook:
    """Кэш цен моделей из /models: цена за токен (вход, выход)."""

    def __init__(self) -> None:
        self._prices: dict[str, tuple[float, float]] = {}
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
                self._prices[item.get("id", "")] = (prompt_price, completion_price)
            self._loaded = True
            log.info("прайс-кэш загружен: %d моделей", len(self._prices))
        except Exception as exc:  # noqa: BLE001 — цены некритичны
            log.warning("не удалось загрузить прайс-кэш: %s", exc)

    def cost(self, model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
        prices = self._prices.get(model)
        if not prices or input_tokens is None or output_tokens is None:
            return None
        prompt_price, completion_price = prices
        return input_tokens * prompt_price + output_tokens * completion_price


_price_book = PriceBook()


async def chat_completion(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float = 0.4,
    provider: dict | None = None,
) -> LLMResponse:
    """Вызов чат-комплишена с одной повторной попыткой. Бросает OpenRouterError."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # Предпочтения провайдеров передаются как есть; пусто = авто-маршрутизация
    if provider:
        payload["provider"] = provider
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
                log.warning("OpenRouter HTTP %s — повтор через %d сек", resp.status_code, delay)
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
        raise OpenRouterError(f"неожиданная структура ответа: {json.dumps(data)[:500]}") from exc

    usage = data.get("usage") or {}
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    answer_model = data.get("model", model)
    return LLMResponse(
        content=content,
        model=answer_model,
        provider=data.get("provider"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=_price_book.cost(answer_model, input_tokens, output_tokens),
        latency_ms=latency_ms,
        finish_reason=finish_reason,
    )