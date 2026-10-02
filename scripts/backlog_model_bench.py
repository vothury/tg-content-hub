"""Бенч моделей-экстракторов на эталонных сэмплах (scripts/samples/sample*.json).

Каждая модель из --models получает боевой промпт BACKLOG_FACTS_SYSTEM и готовый
user-message из сэмпла, отвечает БЕЗ ротации/фолбэков (чистое качество модели)
и оценивается по эталону REFERENCE: покрытие релевантных пунктов, точность
объекта, полнота ключевых токенов фактов, отсутствие годов позже даты поста,
точность против шумовых пунктов (вода/ЖКХ/foreign/вторичка/цитаты).

Запуск без пересборки образа (файл и сэмплы монтируются):
  docker compose run --rm \
    -v ./scripts/backlog_model_bench.py:/app/scripts/backlog_model_bench.py:ro \
    -v ./scripts/samples:/app/scripts/samples:ro \
    --entrypoint python reader scripts/backlog_model_bench.py \
    --models "openai/gpt-oss-20b (darkbloom/fp8), openai/gpt-oss-120b (dekallm/bf16)" \
    --reasoning 3000
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

from app.services.llm.openrouter import OpenRouterError, chat_completion
from app.services.llm.prompts import BACKLOG_FACTS_SYSTEM
from app.services.llm.schemas import BacklogFactsResult, LLMParseError
from app.services.llm_pipeline import _parse_model_spec, _split_model_list

ITEM_RE = re.compile(r"(?m)^(\d+)\. \[(\d{2})\.(\d{2})\.(\d{4})\]")

# Эталон: i -> (объект, ключевые токены фактов; "|" внутри токена = альтернативы)
REFERENCE = {
    "sample1": {
        2: ("ГК ФСК", ["15,5", "221", "70", "7,7", "2025"]),
        3: ("Царицыно", ["2006", "2017", "2018", "35", "400"]),
        4: ("КРТ Подмосковья", ["250", "54", "8"]),
        11: ("льготная ипотека РФ", ["1 трлн|1000 млрд"]),
        19: ("ипотека РФ", ["222", "60", "22"]),
    },
    "sample2": {
        1: ("ипотека РФ", ["350", "1,5"]),
        2: ("Метриум", ["440", "350", "17,7", "14", "18,7", "20", "222"]),
        3: ("Kalinka", ["1,3", "583", "46", "251", "17", "84", "33"]),
        4: ("Швабе|РТ-Капитал", ["16,5", "0,9", "1,8", "1,77", "1,57", "25.09|25 сентября"]),
        5: ("Росреестр", ["99", "6,8", "44,4", "97"]),
        11: ("Дом.РФ", ["50", "1", "58", "82", "69", "74", "59", "71", "148"]),
        12: ("Мир квартир", ["28", "14,1", "205,4", "198,8"]),
        16: ("Домклик", ["22,1", "16,8", "14,7", "14,1", "13,8", "6,6", "4,9"]),
        17: ("Эталон", ["13,2", "54,4", "9,2", "28", "47,9", "23"]),
        18: ("Дом.РФ", ["59", "186", "1,9", "2,6", "2023", "39", "38"]),
        20: ("Will Towers|Upside", ["4,9", "1,7", "181,1"]),
        21: ("Инград", ["3,6", "21,7", "8", "139,9"]),
        22: ("Самолет", ["22,3", "1,8", "117,4", "31", "31,3", "40", "424,6", "14"]),
        23: ("Самолет", ["5,9", "4,4", "10,8", "22"]),
        25: ("ЛСР", ["57,6", "23", "97,1", "0,7", "529,9"]),
    },
    "sample3": {
        1: ("26 ПаркВью|MR", ["32,3", "16", "257", "18,3"]),
        4: ("Инком", ["42", "6,4", "26"]),
        5: ("А101", ["41,1", "19", "177,8", "18", "52,2", "268,9", "159"]),
        8: ("ПИК", ["2,4", "68,768|68,7", "769,225|769,2", "14", "120,088|120,1"]),
        9: ("стройматериалы", ["1 мая|01.05", "2026"]),
        11: ("АПРИ", ["2,2", "8", "25", "7", "9,6", "26", "38", "4,0|4"]),
        12: ("Циан", ["67", "48", "19", "35", "25", "22", "11"]),
        14: ("Дом.РФ", ["18", "3,4", "13,9", "19"]),
        16: ("Дом.РФ", ["26", "3", "119,3", "16,3", "8,2"]),
        19: ("Коммерсант", ["75,5", "1,5", "29,2", "560", "19,6", "1,2", "15,7", "4,1"]),
        20: ("Зеленопарк|РАД", ["123,6", "37", "6,5", "7", "5 июня|05.06"]),
        21: ("Домклик", ["99", "17", "14", "40"]),
        25: ("Домклик", ["39", "16,6", "20", "131,5", "70,4", "51,4", "255,7"]),
    },
}
NOISE = {
    "sample1": {1, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 18, 20, 21, 22, 23, 24, 25},
    "sample2": {4, 6, 7, 8, 9, 10, 13, 14, 15, 19},
    "sample3": {2, 3, 6, 7, 10, 13, 15, 17, 18, 20, 22, 23, 24},
}
OPTIONAL = {"sample2": {24}}   # дубли: извлечение не штрафует и не поощряет


def _tok_hit(tok: str, hay: str) -> bool:
    for v in tok.split("|"):
        if v.lower() in hay or v.replace(",", ".").lower() in hay:
            return True
    return False


_FACTS_LINE_RE = re.compile(r"(?m)^\s*(\d+)\s*[).:]?\s*\|([^|\n]*)\|([^\n]*)$")


def _parse_lenient(content: str) -> list:
    """Спасение обрезанного ответа: полные линии считаем, хвост теряем честно."""
    items, seen = [], set()
    for m in _FACTS_LINE_RE.finditer(content or ""):
        i = int(m.group(1))
        if i in seen:
            continue
        seen.add(i)
        facts = [f.strip() for f in m.group(3).split(";") if f.strip()][:6]
        if facts:
            items.append({"i": i, "rel": True, "obj": m.group(2).strip(), "facts": facts})
    return items


def _obj_score(ref_obj: str, got_obj: str) -> float:
    a, b = ref_obj.lower(), (got_obj or "").lower()
    if any(p in b for p in a.split("|")):
        return 1.0
    r = difflib.SequenceMatcher(None, a.split("|")[0], b).ratio()
    return 1.0 if r >= 0.5 else (0.5 if r >= 0.3 else 0.0)


def _post_years(content: str) -> dict:
    return {int(i): int(y) for i, _d, _m, y in ITEM_RE.findall(content)}


def score_sample(stem: str, content: str, items: list) -> dict:
    ref, noise = REFERENCE[stem], NOISE[stem]
    optional = OPTIONAL.get(stem, set())
    years = _post_years(content)
    got = {it["i"]: it for it in items}
    covered, obj_s, fact_s, wrong_year = [], [], [], 0
    for i, (robj, rtoks) in ref.items():
        g = got.get(i)
        if g is None:
            continue
        covered.append(i)
        obj_s.append(_obj_score(robj, g["obj"]))
        hay = " ".join(g["facts"]).lower()
        hit = sum(1 for t in rtoks if _tok_hit(t, hay))
        fact_s.append(hit / len(rtoks))
        for y in re.findall(r"20\d{2}", " ".join(g["facts"])):
            if int(y) > years.get(i, 9999):
                wrong_year += 1
    missed = sorted(set(ref) - set(covered))
    fp = sorted(set(got) & noise)
    extra = sorted(set(got) - set(ref) - noise - optional)
    recall = len(covered) / len(ref)
    fact = sum(fact_s) / len(fact_s) if fact_s else 0.0
    obj = sum(obj_s) / len(obj_s) if obj_s else 0.0
    score = 100 * (0.40 * recall + 0.30 * fact + 0.15 * obj
                   + 0.15 * max(0.0, 1.0 - wrong_year / max(1, len(items))))
    prec = (len(covered) + len(set(got) & optional)) / max(1, len(got))
    return {"score": score, "recall": recall, "fact": fact, "obj": obj,
            "precision": prec, "missed": missed, "fp": fp, "extra": extra,
            "wrong_year": wrong_year}


async def run_model(spec: str, samples: list, reasoning: int, out_dir: Path,
                    effort: str = "") -> dict:
    slug, prov = _parse_model_spec(spec)
    rows, cost, secs, fails = [], 0.0, 0.0, 0
    for stem, content in samples:
        t0 = time.time()
        items, err = [], None
        try:
            reason_cap = 0 if effort == "none" else min(reasoning, 6000)
            total_cap = 1200 + reason_cap              # max_tokens у OR включает рассуждения
            resp = await chat_completion(
                [{"role": "system", "content": BACKLOG_FACTS_SYSTEM},
                 {"role": "user", "content": content}],
                slug, total_cap, 0.1, provider=prov,
                reasoning_max_tokens=reason_cap or None,
                reasoning_effort=effort or None)
            cost += float(resp.cost_usd or 0.0)
            try:
                items = BacklogFactsResult.from_response(resp.content).items
            except LLMParseError as pe:
                if "обрезан" not in str(pe):
                    raise
                items = _parse_lenient(resp.content)   # salvage: считаем то, что доехало
                truncated = True
            safe = re.sub(r"[^0-9A-Za-z._-]+", "_", spec.strip())
            (out_dir / f"{safe}__{stem}.txt").write_text(
                resp.content or "", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — сбои вызова и парсинга считаем отдельно
            err = f"{exc.__class__.__name__}: {str(exc)[:120]}"
            fails += 1
        secs += time.time() - t0
        if err:
            m = {"score": 0.0, "recall": 0.0, "fact": 0.0, "obj": 0.0, "precision": 0.0,
                 "missed": sorted(REFERENCE[stem]), "fp": [], "extra": [],
                 "wrong_year": 0}
        else:
            m = score_sample(stem, content, items)
        m["error"] = err
        rows.append((stem, m))
    mean = sum(m["score"] for _, m in rows) / max(1, len(rows))
    return {"slug": slug, "spec": spec, "rows": rows, "mean": mean, "cost": cost,
            "secs": secs, "fails": fails}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--reasoning", type=int, default=3000)
    ap.add_argument("--samples", default="scripts/samples/sample*.json")
    ap.add_argument("--out-dir", default="backlog/bench")
    ap.add_argument("--tag", default="",
                    help="метка прогона: сырьё и сводка кладутся в <out-dir>/<tag>/; "
                         "по умолчанию временной штамп — прогоны не перетирают друг друга")
    ap.add_argument("--effort", default="",
                    help="reasoning.effort вместо бюджета токенов ('none' = без рассуждений); "
                         "пусто = прежняя работа через --reasoning")
    args = ap.parse_args()
    stamp = args.tag or time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    samples = []
    for p in sorted(glob.glob(args.samples)):
        stem = Path(p).stem
        if stem not in REFERENCE:
            print(f"пропуск {stem}: для файла нет эталона в REFERENCE")
            continue
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        samples.append((stem, d["content"] if isinstance(d, dict) else str(d)))
    if not samples:
        raise SystemExit("не найдено ни одного сэмпла с эталоном: "
                         "проверьте --samples и монтирование scripts/samples")
    results = []
    for spec in _split_model_list(args.models):
        r = await run_model(spec, samples, args.reasoning, out_dir, args.effort)
        results.append(r)
        print(f"\n=== {r['spec']} | effort={args.effort or 'tokens'} | "
              f"среднее {r['mean']:.1f}/100 | ${r['cost']:.4f} | {r['secs']:.0f}s | сбоев {r['fails']}")
        for stem, m in r["rows"]:
            print(f"  {stem}: {m['score']:5.1f} | recall {m['recall']:.2f} | "
                  f"facts {m['fact']:.2f} | obj {m['obj']:.2f} | prec {m['precision']:.2f} | "
                  f"пропуски {m['missed']} | шум-ложные {m['fp']} | лишние {m['extra']} | "
                  f"год>поста {m['wrong_year']}"
                  + (f" | ERR {m['error']}" if m.get("error") else ""))
    head = " | ".join(s for s, _ in samples)
    table = [f"| модель | {head} | среднее | $ | сек | сбоев |",
             "|---" * (len(samples) + 5) + "|"]
    print(f"\n{table[0]}")
    print(table[1])
    for r in results:
        cells = " | ".join(f"{m['score']:.0f}" for _, m in r["rows"])
        line = (f"| {r['spec']} | {cells} | {r['mean']:.1f} | "
                f"{r['cost']:.4f} | {r['secs']:.0f} | {r['fails']} |")
        table.append(line)
        print(line)
    (out_dir / "summary.md").write_text(
        f"# bench {stamp} | reasoning {args.reasoning} | effort {args.effort or '-'} | samples: "
        + ", ".join(s for s, _ in samples) + "\n\n" + "\n".join(table) + "\n",
        encoding="utf-8")
    print(f"\nсводка и сырьё прогона: {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())