"""Проба прайс-бука: мини-вызов на каждую пару; печатает фактического провайдера,
токены, каталожную и ручную стоимости. Истина по деньгам — активность OpenRouter
(сравните с её строкой за тот же момент); probe даёт id вызова и расчёт обеих цен.

  docker compose run --rm --entrypoint python reader scripts/price_probe.py \
    --pairs "z-ai/glm-5.3-flash (gmicloud/fp8); xiaomi/mimo-v2.6-pro (xiaomi/fp8)"
"""
from __future__ import annotations

import argparse
import asyncio

from app.services.llm.openrouter import _price_book, chat_completion
from app.services.llm_pipeline import _parse_model_spec


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True,
                    help='спецификации через ";": "slug (pin)"')
    args = ap.parse_args()
    for spec in [s.strip() for s in args.pairs.split(";") if s.strip()]:
        slug, prov = _parse_model_spec(spec)
        r = await chat_completion([{"role": "user", "content": "ping"}],
                                  slug, 8, 0.0, provider=prov)
        cat, _ = _price_book.cost(r.model, r.input_tokens, r.output_tokens,
                                  r.cached_tokens)
        book, src = _price_book.cost(r.model, r.input_tokens, r.output_tokens,
                                     r.cached_tokens, r.provider)
        print(f"{spec} | served={r.provider} | in={r.input_tokens} "
              f"out={r.output_tokens} | catalog=${cat} | book=${book} ({src})")


if __name__ == "__main__":
    asyncio.run(main())