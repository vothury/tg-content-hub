"""Контроль сопоставления: каждый факт должен лексически пересекаться
со своим исходным постом (числа/названия) по msg_id в raw-файле.
Расхождение — признак сбоя нумерации в батче.

Запуск:
  python3 scripts/backlog_validate.py backlog/facts_*.kept.jsonl
  python3 scripts/backlog_validate.py backlog/facts_*.kept.jsonl --log backlog/validate_mismatches.log
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


def toks(s: str) -> set:
    """Токены для сравнения: слова >=4 букв, числа >=2 цифр, ё→е."""
    s = (s or "").lower().replace("ё", "е")
    return set(re.findall(r"[а-яa-z]{4,}|\d{2,}", s))


_PERIOD_RE = re.compile(r"^\d{2}\.\d{4}$")
_YEAR_RE = re.compile(r"^20\d{2}$")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Сверка фактов kept с полными текстами raw.")
    ap.add_argument("files", nargs="+",
                    help="backlog/facts_*.kept.jsonl")
    ap.add_argument("--log", default="",
                    help="файл для пар FACT/POST с расхождением")
    args = ap.parse_args()

    total_bad = 0
    total_rows = 0
    log_fh = open(args.log, "w", encoding="utf-8") if args.log else None

    for kf in args.files:
        kp = Path(kf)
        rf = kp.with_name(kp.name.replace(".kept.jsonl", ".jsonl"))
        if not rf.exists():
            print(f"{kp.name}: raw-файл {rf.name} не найден — пропуск")
            continue
        raw = {}
        for ln in rf.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                r = json.loads(ln)
                raw[r["id"]] = r
        kept = [json.loads(ln)
                for ln in kp.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        miss = bad = 0
        suspects = []
        for r in kept:
            src = raw.get(r["msg_id"])
            if src is None:
                miss += 1
                continue
            ft = toks(str(r.get("obj", "")) + " " + " ".join(r.get("facts") or []))
            st = toks(src.get("text", "") or "")
            # Фильтр артефактов: периоды-префиксы ("11.2023:") и годы из даты поста
            # не считаем браком, если остальное совпадает
            ft_filtered = {t for t in ft
                           if not _PERIOD_RE.match(t) and not _YEAR_RE.match(t)}
            st_filtered = st | {r["date"][:4]}  # год поста в текст
            if not (ft_filtered & st_filtered):
                bad += 1
                if log_fh:
                    log_fh.write(
                        f"=== {kp.name} msg_id={r['msg_id']} date={r['date'][:10]}\n"
                        f"OBJ: {r.get('obj', '')}\n"
                        f"FACTS: {'; '.join(r.get('facts', []))}\n"
                        f"POST: {(src.get('text', '') or '')[:300]}\n\n")
                if len(suspects) < 5:
                    suspects.append((r["msg_id"], r.get("obj"),
                                     (r.get("facts") or [""])[:1]))
        total_bad += bad
        total_rows += len(kept)
        pct = (bad / len(kept) * 100) if kept else 0.0
        print(f"{kp.name}: строк {len(kept)}, нет в raw {miss}, "
              f"без пересечения с текстом {bad} ({pct:.1f}%)")
        for s in suspects:
            print("   SUSPECT", s)

    if log_fh:
        log_fh.close()

    pct_total = (total_bad / total_rows * 100) if total_rows else 0.0
    print(f"\nИТОГО: {total_bad}/{total_rows} ({pct_total:.1f}%) подозрительных")
    print("Норма: ≤5-6% (базовая линия артефактов периодов)")
    if args.log:
        print(f"Лог пар для разбора: {args.log}")


if __name__ == "__main__":
    main()
