"""Проверка OpenRouter и слагов моделей до обработки постов: make llm-check."""
from __future__ import annotations

import asyncio

from app.common.logging import setup_logging
from app.config import settings
from app.db.session import session_scope
from app.services.llm.openrouter import OpenRouterError, chat_completion
from app.services.settings import Keys, get_setting

log = setup_logging("llm-check")

TEST_MESSAGES = [
    {"role": "user", "content": 'Ответь строго в JSON: {"ok": true}. Без других слов.'}
]


async def check(stage: str, model: str) -> bool:
    print(f"--- {stage}: {model}")
    try:
        resp = await chat_completion(TEST_MESSAGES, model, max_tokens=50, temperature=0.0)
    except OpenRouterError as exc:
        print(f"ОШИБКА: {exc}")
        return False
    cost = f"${resp.cost_usd:.6f}" if resp.cost_usd is not None else "нет данных"
    print(f"ОК: вход={resp.input_tokens} выход={resp.output_tokens} токенов, стоимость {cost}, {resp.latency_ms} мс")
    print(f"Ответ модели: {resp.content[:200]!r}")
    return True


async def main() -> None:
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY не задан в .env")
        raise SystemExit(1)
    async with session_scope() as session:
        classify_model = str(await get_setting(session, Keys.CLASSIFY_MODEL))
        rewrite_model = str(await get_setting(session, Keys.REWRITE_MODEL))
    ok_classify = await check("классификация", classify_model)
    ok_rewrite = await check("рерайт", rewrite_model)
    if not (ok_classify and ok_rewrite):
        print("Есть ошибки. Уточните слаги моделей в каталоге openrouter.ai/models и поправьте .env, затем повторите make llm-check")
        raise SystemExit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())