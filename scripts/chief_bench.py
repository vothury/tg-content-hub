"""Бенч главреда: выбор тем из кластеров заголовков (роль editorial.chief).

Снапшоты: scripts/samples_chief/snap_*.json — недавние темы, кластеры с объектами,
ожидания. Офлайн-прогон боевого промпта CHIEF_SYSTEM/CHIEF_USER:
  docker compose run --rm \
    -v ./scripts/chief_bench.py:/app/scripts/chief_bench.py:ro \
    -v ./scripts/samples_chief:/app/scripts/samples_chief:ro \
    -v ./app/services/llm/prompts.py:/app/app/services/llm/prompts.py:ro \
    --entrypoint python reader scripts/chief_bench.py \
    --models "z-ai/glm-5.3-flash (gmicloud/fp8), anthropic/claude-haiku-5.5" --tag chief-v1
Рубрика: тяжёлые 0.25 (дубль недавней темы, смешивание объектов, не-дроп пустой
ленты, scope без оговорки, контракт), лёгкие 0.1 (опора без цифр).
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
from app.services.llm.prompts import CHIEF_SYSTEM, CHIEF_USER
from app.services.llm.schemas import ChiefTopicsResult
from app.services.llm_pipeline import _parse_model_spec, _split_model_list

TOTAL = 2500
REASON = 400
MAX_TOPICS = 2
SCOPE_WORDS = ("росси", "по стране", "общероссийск", "в стране")


def _tget(t, k):
    return t.get(k) if isinstance(t, dict) else getattr(t, k, None)


def _sig_tokens(s: str) -> set:
    return set(re.findall(r"[a-zа-яё]{6,}", s.lower()))


def _overlap(a: str, b: str) -> float:
    ta, tb = _sig_tokens(a), _sig_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _checks(snap: dict, topics: list) -> list:
    bad = []
    cl = {c["i"]: c for c in snap["clusters"]}
    recent = snap.get("recent") or []
    if len(topics) > MAX_TOPICS:
        bad.append("contract:topics>2")
    for t in topics:
        ids = [int(i) for i in (_tget(t, "headlines") or [])]
        if not ids or any(i not in cl for i in ids):
            bad.append("contract:bad_idx")
            continue
        sel = [cl[i] for i in ids]
        if not any(re.search(r"\d", c["title"]) for c in sel):
            bad.append("no_digits_grounding")
        objs = {c.get("obj") for c in sel if c.get("obj")}
        if len(objs) > 1:
            bad.append("mix_objects")
        text = f"{_tget(t, 'theme') or ''} {_tget(t, 'hypothesis') or ''}"
        if any(_overlap(text, r) > 0.6 for r in recent):
            bad.append("dup_recent")
        if objs == {"rf"} and not any(w in text.lower() for w in SCOPE_WORDS):
            bad.append("scope_no_caveat")
    if snap.get("expect_weak") and topics:
        bad.append("weak_not_dropped")
    return bad


async def run_snap(spec: str, snap: dict):
    slug, prov = _parse_model_spec(spec)
    listing = "\n".join(f"{c['i']}. {c['title']}" for c in snap["clusters"])
    recent = "\n".join(f"- {x}" for x in snap.get("recent") or []) or "—"
    for attempt in (1, 2, 3):
        try:
            r = await chat_completion(
                [{"role": "system", "content": CHIEF_SYSTEM.format(max_topics=MAX_TOPICS)},
                 {"role": "user", "content": CHIEF_USER.format(recent=recent, listing=listing)}],
                slug, TOTAL, 0.1, provider=prov, reasoning_max_tokens=REASON)
            res = ChiefTopicsResult.from_response(r.content)
            return res.topics, None
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                return None, f"{exc.__class__.__name__}: {exc}"
            await asyncio.sleep(2)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--snaps", default="scripts/samples_chief/snap_*.json")
    ap.add_argument("--tag", default="chief-v1")
    args = ap.parse_args()
    snaps = []
    for p in sorted(glob.glob(args.snaps)):
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        d["file"] = Path(p).stem
        snaps.append(d)
    print(f"снапшотов в наборе: {len(snaps)}")
    for spec in _split_model_list(args.models):
        scores, skips = [], 0
        for s in snaps:
            t0 = time.time()
            topics, err = await run_snap(spec, s)
            if err is not None:
                skips += 1
                print(f"\n===== {spec} | {s['file']} | SKIP ({err})")
                continue
            bad = _checks(s, topics)
            light = [b for b in bad if b == "no_digits_grounding"]
            heavy = [b for b in bad if b not in light]
            score = max(0.0, 1.0 - 0.25 * len(heavy) - 0.1 * len(light))
            scores.append(score)
            print(f"\n===== {spec} | {s['file']} | тем {len(topics)} | "
                  f"score {score:.2f} | {time.time() - t0:.0f}s"
                  + (f" | FAIL: {', '.join(bad)}" if bad else ""))
            for t in topics:
                print(f"  - [{_tget(t, 'kind')}] {_tget(t, 'theme')} | "
                      f"{_tget(t, 'hypothesis')} | кластеры {_tget(t, 'headlines')}")
        mean = sum(scores) / max(1, len(scores))
        print(f"\n##### {spec} | среднее рубрики chief: {mean:.2f} из 1.00 "
              f"({len(scores)} снапшотов оценено, пропусков {skips})")


if __name__ == "__main__":
    asyncio.run(main())