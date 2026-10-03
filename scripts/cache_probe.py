"""Зонд prompt-кэша на РЕАЛЬНЫХ данных: неизменяемая часть (BACKLOG_FACTS_SYSTEM
+ шаблон BACKLOG_SCAN_USER) и изменяемая часть (листинг настоящих постов из
backlog/facts_*.jsonl, формат и объём как в боевом facts-режиме: 25 постов,
текст [:700], нумерация и дата [дд.мм.гггг]).

Серия А: без пинна провайдера и без session_id (авто-роутинг) — кэш не живёт,
         запросы расползаются по провайдерам.
Серия Б: пинн провайдера + session_id (как в бою) — кэш виден как
         cached_tokens > 0 начиная со второго вызова (cached ≈ размер системника),
         стоимость падает, если провайдер публикует цену cache_read.

Запуск:
  docker compose run --rm -v ./scripts/cache_probe.py:/app/scripts/cache_probe.py:ro \
    --entrypoint python reader scripts/cache_probe.py --provider streamlake/fp8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime as _dt
from pathlib import Path

from app.services.llm.openrouter import chat_completion
from app.services.llm.prompts import BACKLOG_FACTS_SYSTEM as SYS
from app.services.llm.prompts import BACKLOG_SCAN_USER as USER_TPL


def _real_batches(calls: int, batch: int) -> list[str]:
    """Последовательные батчи листингов из реальных raw-файлов скана."""
    lines: list = []
    for p in sorted(Path('backlog').glob('facts_*.jsonl')):
        for ln in p.read_text(encoding='utf-8').splitlines():
            if not ln.strip():
                continue
            r = json.loads(ln)
            if (r.get('text') or '').strip():
                lines.append(r)
        if len(lines) >= calls * batch:
            break
    batches = []
    for b in range(calls):
        chunk = lines[b * batch:(b + 1) * batch]
        if not chunk:
            break
        listing = "\n".join(
            f"{n}. [{_dt.fromisoformat(e['date']).strftime('%d.%m.%Y')}] "
            f"{(e['text'] or '')[:700]}"
            for n, e in enumerate(chunk, 1))
        batches.append(USER_TPL.format(listing=listing))
    return batches


async def series(title: str, sid, provider_spec, batches, model, reasoning):
    print(f'--- {title} | session_id={sid} | provider={provider_spec}')
    tot_cost, tot_cached = 0.0, 0
    for i, user_content in enumerate(batches):
        t0 = time.monotonic()
        r = await chat_completion(
            [{'role': 'system', 'content': SYS},
             {'role': 'user', 'content': user_content}],
            model, 120, 0.1,
            provider=provider_spec,
            reasoning_max_tokens=reasoning,
            session_id=sid)
        dt = time.monotonic() - t0
        cached = getattr(r, 'cached_tokens', None) or 0
        tot_cached += cached
        tot_cost += r.cost_usd or 0.0
        cost = f'${r.cost_usd:.6f}' if r.cost_usd is not None else 'n/a'
        print(f'  батч {i+1}: cached={cached} in={r.input_tokens} out={r.output_tokens} '
              f'cost={cost} lat={dt:.1f}s prov={r.provider}')
        await asyncio.sleep(1.0)
    print(f'  итог серии: cost=${tot_cost:.6f}, cached={tot_cached}')
    return tot_cost, tot_cached


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default='z-ai/glm-5.3-flash')
    ap.add_argument('--provider', default='streamlake/fp8')
    ap.add_argument('--calls', type=int, default=4, help='батчей на серию')
    ap.add_argument('--batch', type=int, default=25, help='постов в батче, как в бою')
    ap.add_argument('--reasoning', type=int, default=100)
    ap.add_argument('--session', default='cacheprobe-streamlake')
    args = ap.parse_args()
    batches = _real_batches(args.calls, args.batch)
    if len(batches) < 2:
        raise SystemExit('нужно минимум 2 реальных батча: проверьте, что в контейнере '
                         'виден backlog/ с facts_*.jsonl (или примонтируйте -v)')
    a_cost, a_cached = await series('А: авто-роутинг, без сессии', None, None,
                                    batches, args.model, args.reasoning)
    b_cost, b_cached = await series('Б: пинн + сессия (боевая схема)', args.session,
                                    {'order': [args.provider], 'allow_fallbacks': False},
                                    batches, args.model, args.reasoning)
    print(f'\nвердикт: серия А cached={a_cached}, серия Б cached={b_cached}; '
          f'стоимость {a_cost:.6f} -> {b_cost:.6f}')
    if b_cached > 0:
        print('кэш РАБОТАЕТ: неизменяемая часть (системник) отдаётся из кэша '
              'на pinned-сессии; изменяемая (листинг) тарифицируется полностью — '
              'это и есть наша боевая экономика')
    else:
        print('кэш инертен на этом провайдере: cached=0 везде — фича безвредна, '
              'экономии здесь нет; повторите с --provider parasail/fp4 или gmicloud/fp8')


if __name__ == '__main__':
    asyncio.run(main())
