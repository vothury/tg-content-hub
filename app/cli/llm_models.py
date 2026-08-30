"""Эндпоинты модели в OpenRouter: провайдеры, цены, квантования.

Использование: make llm-models MODEL=openai/gpt-5.6-luna
Нужно для подбора слагов провайдеров в настройки *_PROVIDERS.

Точный слаг для конкретного эндпоинта лучше всего брать кнопкой копирования
на странице модели в каталоге openrouter.ai/models — команда лишь подсказка.
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

    if resp.status_code == 403:
        print("HTTP 403: этот ключ не может запрашивать список эндпоинтов.")
        print("Откройте модель в каталоге и используйте кнопку копирования слага рядом с провайдером:")
        print(f"  https://openrouter.ai/models/{model}")
        raise SystemExit(1)
    if resp.status_code == 404:
        print(f"HTTP 404: модель '{model}' не найдена — проверьте слаг в каталоге")
        raise SystemExit(1)
    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}: {resp.text[:500]}")
        raise SystemExit(1)

    payload = resp.json().get("data") or {}
    endpoints = payload.get("endpoints") or []
    print(f"Модель: {model} — эндпоинтов: {len(endpoints)}")
    for ep in endpoints:
        provider = ep.get("provider_name") or "?"
        tag = ep.get("tag") or ""
        quant = ep.get("quantization") or "—"
        ctx = ep.get("context_length")
        pricing = ep.get("pricing") or {}
        try:
            price_in = float(pricing.get("prompt", 0)) * 1_000_000
            price_out = float(pricing.get("completion", 0)) * 1_000_000
            price = f"${price_in:.3f} / ${price_out:.3f} за 1M"
        except (TypeError, ValueError):
            price = "цена не указана"
        slug_hint = f" | слаг: {tag}" if tag else ""
        print(f"- провайдер: {provider:<22} квантование: {str(quant):<8} контекст: {ctx} | {price}{slug_hint} | {ep.get('name', '')}")

    print()
    print("Слаги для поля 'order': базовый (например 'azure') покрывает все регионы провайдера,")
    print("а региональные/тировые варианты указывайте целиком: 'amazon-bedrock/us-east-1', 'openai/flex'.")
    print("Сервисные тиры (flex и т.п.) базовым слагом НЕ покрываются — только полный слаг.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("model", help="слаг модели, например openai/gpt-5.6-luna")
    args = parser.parse_args()
    asyncio.run(main(args.model))