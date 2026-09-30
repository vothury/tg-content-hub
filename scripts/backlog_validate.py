"""Контроль сопоставления: каждый факт должен лексически пересекаться
со своим исходным постом (числа/названия) по msg_id в raw-файле.
Расхождение — признак сбоя нумерации в батче.

Запуск на хосте из корня репозитория:
  python3 scripts/backlog_validate.py backlog/facts_*.kept.jsonl
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def toks(s: str) -> set:
    s = (s or "").lower().replace("ё", "е")
    return set(re.findall(r"[а-яa-z]{4,}|\d{2,}", s))


def main() -> None:
    files = sys.argv[1:]
    if not files:
        raise SystemExit("использование: backlog_validate.py <facts_*.kept.jsonl> [...]")
    total_bad = 0
    for kf in files:
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
                for ln in kp.read_text(encoding="utf-8").splitlines() if ln.strip()]
        miss = bad = 0
        suspects = []
        for r in kept:
            src = raw.get(r["msg_id"])
            if src is None:
                miss += 1
                continue
            ft = toks(str(r.get("obj", "")) + " " + " ".join(r.get("facts") or []))
            st = toks(src.get("text", ""))
            if not (ft & st):
                bad += 1
                if len(suspects) < 5:
                    suspects.append((r["msg_id"], r.get("obj"), (r.get("facts") or [""])[:1]))
        total_bad += bad + miss
        pct = (bad / len(kept) * 100) if kept else 0.0
        print(f"{kp.name}: строк {len(kept)}, нет в raw {miss}, "
              f"без пересечения с текстом {bad} ({pct:.1f}%)")
        for s in suspects:
            print("   SUSPECT", s)
    print("ИТОГО подозрительных:", total_bad,
          "— норма <2–3% (модель нормализует числа/имена); при большем sprawdьте нумерацию")


if __name__ == "__main__":
    main()