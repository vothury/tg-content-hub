"""Эндпоинты модели в OpenRouter: провайдеры, цены, квантования.

Использование: make llm-models MODEL=openai/gpt-5.6-luna
Нужно для подбора имён провайдеров в настройки *_PROVIDERS.
"""
from __future__ import annotations

import argparse
import asyncio

import httpx

from app.common.logging import setup_logging
from app.config import settings

log = setup_logging("llm-models")


async def main(model: str) -> None:
    url = f"{settings.openrouter_base_url}/models/{model}/endpoints"
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        print(f"Сетевая ошибка: {exc}")
        raise SystemExit(1)

    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}: не удалось получить эндпоинты '{model}' — проверьте слаг")
        print(resp.text[:500])
        raise SystemExit(1)

    payload = resp.json().get("data") or {}
    endpoints = payload.get("endpoints") or []
    print(f"Модель: {model} — эндпоинтов: {len(endpoints)}")
    for ep in endpoints:
        provider = ep.get("provider_name") or "?"
        name = ep.get("name") or provider
        quant = ep.get("quantization") or ep.get("quantizations") or "—"
        ctx = ep.get("context_length")
        pricing = ep.get("pricing") or {}
        try:
            price_in = float(pricing.get("prompt", 0)) * 1_000_000
            price_out = float(pricing.get("completion", 0)) * 1_000_000
            price = f"${price_in:.3f} / ${price_out:.3f} за 1M"
        except (TypeError, ValueError):
            price = "цена не указана"
        print(f"- провайдер: {provider:<14} квантование: {str(quant):<10} контекст: {ctx} | {price} | {name}")

    print()
    print("Имена провайдеров из этого списка используйте в поле 'order' настроек *_PROVIDERS.")
    print("Список может отличаться от документации — ориентируйтесь на фактический вывод.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("model", help="слаг модели, например openai/gpt-5.6-luna")
    args = parser.parse_args()
    asyncio.run(main(args.model))