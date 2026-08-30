"""Проверка OpenRouter и слагов моделей до обработки постов: make llm-check."""
from __future__ import annotations

import asyncio

from app.common.logging import setup_logging
from app.config import settings
from app.db.session import session_scope
from app.services.llm.openrouter import OpenRouterError, chat_completion
from app.services.settings import Keys, get_providers, get_setting

log = setup_logging("llm-check")

TEST_MESSAGES = [
    {"role": "user", "content": 'Ответь строго в JSON: {"ok": true}. Без других слов.'}
]


async def check(stage: str, model: str, providers: dict | None) -> bool:
    print(f"--- {stage}: {model}")
    if providers:
        print(f"    предпочтения провайдеров: {providers}")
    try:
        resp = await chat_completion(TEST_MESSAGES, model, max_tokens=50, temperature=0.0, provider=providers)
    except OpenRouterError as exc:
        print(f"ОШИБКА: {exc}")
        return False
    cost = f"${resp.cost_usd:.6f}" if resp.cost_usd is not None else "нет данных"
    print(f"ОК: вход={resp.input_tokens} выход={resp.output_tokens} токенов, стоимость {cost}, {resp.latency_ms} мс")
    print(f"Провайдер, выбранный OpenRouter: {resp.provider or 'не указан'}")
    print(f"Ответ модели: {resp.content[:200]!r}")
    return True


async def main() -> None:
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY не задан в .env")
        raise SystemExit(1)
    async with session_scope() as session:
        classify_model = str(await get_setting(session, Keys.CLASSIFY_MODEL))
        rewrite_model = str(await get_setting(session, Keys.REWRITE_MODEL))
        classify_providers = await get_providers(session, Keys.CLASSIFY_PROVIDERS)
        rewrite_providers = await get_providers(session, Keys.REWRITE_PROVIDERS)
    ok_classify = await check("классификация", classify_model, classify_providers)
    ok_rewrite = await check("рерайт", rewrite_model, rewrite_providers)
    if not (ok_classify and ok_rewrite):
        print("Есть ошибки. Уточните слаги в каталоге openrouter.ai/models и повторите make llm-check")
        raise SystemExit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())