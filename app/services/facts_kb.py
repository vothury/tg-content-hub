"""Read-only доступ к базе знаний backlog (backlog/facts.db) для промптов редакции.

Справка не обязательна: нет файла или совпадений — промпт получит "—",
писатель работает только по материалам темы.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

DB_PATH = Path("backlog/facts.db")
_STOP = {"рынок", "новостроек", "москвы", "московской", "области", "россии",
         "ипотека", "семейная", "программа", "проектов", "застройщики"}


def search(theme: str, limit: int = 6) -> list:
    """Топ-факты по 1-3 самым длинным содержательным словам темы."""
    if not DB_PATH.exists():
        return []
    words = [w for w in re.split(r"[^0-9A-Za-zА-Яа-яЁё-]+", theme or "")
             if len(w) >= 6 and w.lower() not in _STOP]
    words = sorted(set(words), key=len, reverse=True)[:3]
    if not words:
        return []
    out, seen = [], set()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        for w in words:
            like = f"%{w}%"
            rows = con.execute(
                "SELECT date, obj, fact, sources FROM facts "
                "WHERE obj LIKE ? OR fact LIKE ? ORDER BY date DESC LIMIT ?",
                (like, like, limit)).fetchall()
            for d, o, f, s in rows:
                key = (d[:10], o, f)
                if key not in seen:
                    seen.add(key)
                    out.append({"date": d[:10], "obj": o, "fact": f, "sources": s})
            if len(out) >= limit:
                break
    except sqlite3.OperationalError:
        out = []
    finally:
        con.close()
    return out[:limit]


def format_context(rows: list) -> str:
    return "\n".join(f"- {r['date']} | {r['obj']}: {r['fact']}" for r in rows) or "—"