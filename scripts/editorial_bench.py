"""Бенч писателя редакции: темы -> writer -> editor -> пост + авто-рубрика.
Офлайн-итерации промптов/моделей/бренд-файлов без деплоя:
  docker compose run --rm \
    -v ./scripts/editorial_bench.py:/app/scripts/editorial_bench.py:ro \
    -v ./scripts/samples_writer:/app/scripts/samples_writer:ro \
    -v ./app/services/llm/prompts.py:/app/app/services/llm/prompts.py:ro \
    -v ./app/services/editorial_brand:/app/app/services/editorial_brand:ro \
    --entrypoint python reader scripts/editorial_bench.py \
    --models "openai/gpt-5.6-luna (openai), z-ai/glm-5.3-flash (gmicloud/fp8)" --tag wide-v1
Кросс-пары (редактор отдельной моделью, как hypothesis-путь прода):
    --models "xiaomi/mimo-v2.6-pro (xiaomi/fp8)" \
    --editor-model "z-ai/glm-5.3-flash (gmicloud/fp8)" --tag wide-v7-cross
Рубрика: знаменатель процента, запрет точности 0,00%, тавтологии-роли, канцелярит,
стена текста, бюджет цифр, повторы лида, слабые глаголы, overedit (вкусовая
переписка чистого черновика), соответствие expect (write/drop/forecast/stale).
Веса: тяжёлые провалы 0.15, лёгкие (precision/numbers/wall/weak_verbs) 0.05.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import glob
import json
import re
import time
from pathlib import Path

from app.services.llm.openrouter import chat_completion
from app.services.llm.prompts import (EDITOR_SYSTEM, EDITOR_USER,
                                      WRITER_SYSTEM, WRITER_USER)
from app.services.llm.schemas import EditorialArticleResult, EditorResult
from app.services.editorial_writer import _EDITOR_SCOPE
from app.services.llm_pipeline import _parse_model_spec, _split_model_list

MAX_CHARS = 2500
REASON = 300
WRITER_TOTAL = 5000   # потолок вместе с рассуждениями: платим только за сгенерированное,
EDITOR_TOTAL = 4000   # а обрезанный вызов = полностью потраченные деньги без результата
WRITER_ATTEMPTS = 3   # попытки на пустой/битый JSON, затем пропуск темы


from app.services.editorial_rubric import rubric_checks

def _checks(topic: dict, title: str, body: str) -> list:
    return rubric_checks(title, body)


def _expect_ok(topic: dict, verdict: str, body: str) -> tuple:
    exp = topic.get("expect", "write")
    if exp == "drop":
        return (verdict == "drop"), f"expect drop, got {verdict}"
    if verdict == "drop":
        return False, "unexpected drop"
    if exp == "forecast":
        ok = any(k in body for k in ("прогноз", "ожида", "может", "ждут", "допуска"))
        return ok, "forecast not attributed"
    if exp == "stale":
        low = body.lower()
        ok = any(k in low for k in ("по данным на", "в марте", "весной",
                                    "по итогам 2025", "за 2025 год", "2025 год",
                                    "годом ранее", "марта 2026"))
        return ok, "stale materials not date-anchored"
    return True, ""


def _brand() -> str:
    d = Path("/app/app/services/editorial_brand")
    parts = []
    for name in ("voice.md", "forbidden.md", "examples_good.md", "examples_bad.md"):
        p = d / name
        if p.exists():
            parts.append(p.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts) or "—"


async def run_topic(spec: str, topic: dict, out_dir: Path, editor_spec: str = ""):
    slug, prov = _parse_model_spec(spec)
    eslug, eprov = _parse_model_spec(editor_spec or spec)
    tier = "strong" if editor_spec else "flash"   # кросс-пара = hypothesis-путь прода
    materials = "\n\n".join(
        f"{n}. [{m['label']}]\n{m['text']}"
        for n, m in enumerate(topic["materials"], 1))
    kb = topic.get("kb") or "—"

    res = None
    for attempt in range(1, WRITER_ATTEMPTS + 1):
        try:
            w = await chat_completion(
                [{"role": "system", "content": WRITER_SYSTEM.format(max_chars=MAX_CHARS)
                  + "\n\nBRAND AND STYLE MEMORY:\n" + _brand()},
                 {"role": "user", "content": WRITER_USER.format(
                     theme=topic["theme"], hypothesis=topic["hypothesis"],
                     kind=topic["kind"], materials=materials, kb=kb)}],
                slug, WRITER_TOTAL, 0.1, provider=prov,
                reasoning_max_tokens=REASON)
            res = EditorialArticleResult.from_response(w.content)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == WRITER_ATTEMPTS:
                print(f"----- writer failed {WRITER_ATTEMPTS}x "
                      f"({exc.__class__.__name__}): SKIP {spec} | {topic['file']}")
                return "skip", "", [], []
            await asyncio.sleep(2)

    if res.verdict == "drop":
        exp = topic.get("expect", "write")
        if exp == "drop":
            return "drop", res.drop_reason, [], []
        return "drop", res.drop_reason, [f"lost_story: expect {exp}, got drop"], []

    body = res.text
    for attempt in (1, 2):
        try:
            e = await chat_completion(
                [{"role": "system", "content": EDITOR_SYSTEM.format(max_chars=MAX_CHARS)
                  + "\n\nBRAND AND STYLE MEMORY:\n" + _brand()
                  + "\n\nEDITOR SCOPE (writer tier: " + tier + "):\n"
                  + _EDITOR_SCOPE[tier]},
                 {"role": "user", "content": EDITOR_USER.format(
                     materials=materials, kb=kb, draft=res.text)}],
                eslug, EDITOR_TOTAL, 0.1, provider=eprov, reasoning_max_tokens=REASON)
            ed = EditorResult.from_response(e.content)
            if ed.verdict == "rewrite" and ed.text:
                body = ed.text
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                print(f"----- editor failed ({exc.__class__.__name__}): draft kept")
            else:
                await asyncio.sleep(2)

    final = f"{res.title.strip()}\n\n{body.strip()}"
    safe = re.sub(r"[^0-9A-Za-z.-]+", "", spec)
    (out_dir / f"{safe}__{topic['file']}.txt").write_text(final, encoding="utf-8")
    draft_bad = _checks(topic, res.title, res.text)
    (out_dir / f"{safe}__{topic['file']}.draft.txt").write_text(
        f"{res.title.strip()}\n\n{res.text.strip()}", encoding="utf-8")
    bad = _checks(topic, res.title, body)
    ok, note = _expect_ok(topic, "write", body)
    if not ok:
        bad.append(note)
    if body != res.text and not draft_bad and difflib.SequenceMatcher(
            None, res.text, body).ratio() < 0.55:
        bad.append("overedit")   # чистый черновик переписан вкусово = нарушение скоупа
    return "write", final, bad, draft_bad


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--topics", default="scripts/samples_writer/topic_*.json")
    ap.add_argument("--tag", default="wbench")
    ap.add_argument("--editor-model", default="",
                    help="модель редактора отдельно от писателя (кросс-пары)")
    args = ap.parse_args()
    out_dir = Path("backlog/wbench") / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    topics = []
    for p in sorted(glob.glob(args.topics)):
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        d["file"] = Path(p).stem
        topics.append(d)
    print(f"тем в наборе: {len(topics)}")
    for spec in _split_model_list(args.models):
        scores = []
        skips = 0
        for t in topics:
            t0 = time.time()
            verdict, final, bad, draft_bad = await run_topic(
                spec, t, out_dir, args.editor_model)
            if verdict == "skip":
                skips += 1
                print(f"\n===== {spec} | {t['file']} | SKIP (writer empty after retries)")
                continue
            light = [b for b in bad if b.startswith(
                ("precision", "numbers", "wall", "weak_verbs"))]
            heavy = [b for b in bad if b not in light]
            score = max(0.0, 1.0 - 0.15 * len(heavy) - 0.05 * len(light))
            scores.append(score)
            dlight = [b for b in draft_bad if b.startswith(
                ("precision", "numbers", "wall", "weak_verbs"))]
            dscore = max(0.0, 1.0 - 0.15 * (len(draft_bad) - len(dlight))
                         - 0.05 * len(dlight))
            print(f"\n===== {spec} | {t['file']} | {verdict} | "
                  f"draft {dscore:.2f} -> final {score:.2f} | {time.time() - t0:.0f}s"
                  + (f" | FAIL: {', '.join(bad)}" if bad else ""))
            if verdict == "write":
                print(final)
            else:
                print(f"DROP: {final}")
        mean = sum(scores) / max(1, len(scores))
        print(f"\n##### {spec} | среднее по рубрике: {mean:.2f} из 1.00 "
              f"({len(scores)} тем оценено, пропусков {skips})")


if __name__ == "__main__":
    asyncio.run(main())