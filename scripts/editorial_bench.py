"""Бенч писателя редакции: файлы тем -> writer -> editor -> финальный пост.

Офлайн-итерации промптов и моделей БЕЗ деплоя и без трогания редакции:
локальный prompts.py монтируется в образ, эталоны лежат рядом с темами.
  docker compose run --rm \
    -v ./scripts/editorial_bench.py:/app/scripts/editorial_bench.py:ro \
    -v ./scripts/samples_writer:/app/scripts/samples_writer:ro \
    -v ./app/services/llm/prompts.py:/app/app/services/llm/prompts.py:ro \
    --entrypoint python reader scripts/editorial_bench.py \
    --models "openai/gpt-5.6-luna (openai), z-ai/glm-5.3-flash (gmicloud/fp8)" --tag style-v3
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import re
import time
from pathlib import Path

from app.services.llm.openrouter import chat_completion
from app.services.llm.prompts import (EDITOR_SYSTEM, EDITOR_USER,
                                      WRITER_SYSTEM, WRITER_USER)
from app.services.llm.schemas import EditorialArticleResult, EditorResult
from app.services.llm_pipeline import _parse_model_spec, _split_model_list

MAX_CHARS = 2500
REASON = 300


async def run_topic(spec: str, topic: dict, out_dir: Path) -> str:
    slug, prov = _parse_model_spec(spec)
    materials = "\n\n".join(
        f"{n}. [{m['label']}]\n{m['text']}"
        for n, m in enumerate(topic["materials"], 1))
    kb = topic.get("kb") or "—"
    w = await chat_completion(
        [{"role": "system", "content": WRITER_SYSTEM.format(max_chars=MAX_CHARS)},
         {"role": "user", "content": WRITER_USER.format(
             theme=topic["theme"], hypothesis=topic["hypothesis"],
             kind=topic["kind"], materials=materials, kb=kb)}],
        slug, 1400 + REASON, 0.1, provider=prov, reasoning_max_tokens=REASON)
    res = EditorialArticleResult.from_response(w.content)
    if res.verdict == "drop":
        return f"VERDICT DROP: {res.drop_reason}"
    e = await chat_completion(
        [{"role": "system", "content": EDITOR_SYSTEM.format(max_chars=MAX_CHARS)},
         {"role": "user", "content": EDITOR_USER.format(
             materials=materials, kb=kb, draft=res.text)}],
        slug, 1200 + REASON, 0.1, provider=prov, reasoning_max_tokens=REASON)
    ed = EditorResult.from_response(e.content)
    body = ed.text if (ed.verdict == "rewrite" and ed.text) else res.text
    final = f"{res.title.strip()}\n\n{body.strip()}"
    safe = re.sub(r"[^0-9A-Za-z.-]+", "", spec)
    (out_dir / f"{safe}__{topic['file']}.txt").write_text(final, encoding="utf-8")
    return final


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--topics", default="scripts/samples_writer/topic_*.json")
    ap.add_argument("--tag", default="wbench")
    args = ap.parse_args()
    out_dir = Path("backlog/wbench") / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    topics = []
    for p in sorted(glob.glob(args.topics)):
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        d["file"] = Path(p).stem
        topics.append(d)
    for spec in _split_model_list(args.models):
        for t in topics:
            t0 = time.time()
            final = await run_topic(spec, t, out_dir)
            print(f"\n===== {spec} | {t['file']} | {time.time() - t0:.0f}s\n{final}")


if __name__ == "__main__":
    asyncio.run(main())