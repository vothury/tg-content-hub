"""Замер выгоды перевода промптов на EN: 3 конфигурации на одних и тех же постах.

A = текущий RU-каркас, рассуждения RU (как сейчас)
B = EN-каркас, рассуждения EN, ответ RU
C = EN-каркас, рассуждения EN, ответ EN
Печатает input/output/стоимость и экономию B,A и C,A. Расход — несколько центов.

Запуск: docker compose run --rm --entrypoint python api scripts/prompt_lang_bench.py [--samples 3] [--model SLUG]
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.config import settings
from app.db.models import Post
from app.db.session import session_scope
from app.services.llm.openrouter import chat_completion
from app.services.llm.prompts import CLASSIFY_USER, build_classify_prompt
from app.services.settings import Keys, get_setting, get_providers

EN_SYSTEM_TEMPLATE = (
    "You are a strict editor-classifier for a Telegram repost hub.\n"
    "Decide whether the source post fits the target channel and score it 0-10.\n"
    "Channel: {channel_title}. Topic relevance notes: {relevance}.\n"
    "Return ONLY JSON with keys: canonical (str), suitable (bool), score (number), "
    "category (str), reason (str), risks (list of str).\n"
    "LANGUAGE RULES: think and reason in English; write JSON string values in "
    "{response_lang}; canonical MUST stay in the same language as the source text; "
    "return ONLY valid JSON, no markdown fences.\n"
)


async def _samples(n: int) -> list:
    async with session_scope() as session:
        rows = (await session.execute(
            select(Post.id, Post.original_text)
            .where(Post.original_text.isnot(None))
            .order_by(Post.id.desc()).limit(n))).all()
    return [(int(r[0]), (r[1] or "")[:4000]) for r in rows]


async def _run(messages: list, model: str, providers) -> tuple:
    resp = await chat_completion(messages, model, 1200, temperature=0.2,
                                 provider=providers,
                                 reasoning_max_tokens=settings.llm_reasoning_max_tokens)
    return resp.input_tokens or 0, resp.output_tokens or 0, resp.cost_usd or 0.0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--model", default="")
    args = ap.parse_args()

    async with session_scope() as session:
        model = args.model or str(await get_setting(session, Keys.CLASSIFY_MODEL))
        providers = await get_providers(session, Keys.CLASSIFY_PROVIDERS)
    samples = await _samples(args.samples)
    totals = {"A": [0, 0, 0.0], "B": [0, 0, 0.0], "C": [0, 0, 0.0]}

    for pid, text in samples:
        ru_system = build_classify_prompt(
            channel_title="тест-канал", channel_description=None, relevance=None,
            verbose=False, media_hint=None, source_note=None,
            source_username=None, source_title=None, channel_note=None)
        configs = {
            "A": (ru_system, CLASSIFY_USER.format(text=text)),
            "B": (EN_SYSTEM_TEMPLATE.format(channel_title="test channel",
                                            relevance="none", response_lang="Russian"),
                  CLASSIFY_USER.format(text=text)),
            "C": (EN_SYSTEM_TEMPLATE.format(channel_title="test channel",
                                            relevance="none", response_lang="English"),
                  CLASSIFY_USER.format(text=text)),
        }
        for key, (system, user) in configs.items():
            inp, out, cost = await _run(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model, providers)
            totals[key][0] += inp
            totals[key][1] += out
            totals[key][2] += cost
            print(f"пост {pid} [{key}] input={inp} output={out} cost=${cost:.6f}")

    print("-" * 70)
    for key in ("A", "B", "C"):
        inp, out, cost = totals[key]
        print(f"{key}: input={inp} output={out} cost=${cost:.6f}")
    a_in, a_out, a_cost = totals["A"]
    for key in ("B", "C"):
        inp, out, cost = totals[key]
        di = (1 - inp / a_in) * 100 if a_in else 0
        do = (1 - out / a_out) * 100 if a_out else 0
        dc = (1 - cost / a_cost) * 100 if a_cost else 0
        print(f"экономия {key} vs A: input {di:.1f}%, output {do:.1f}%, стоимость {dc:.1f}%")


asyncio.run(main())