"""База знаний backlog: sqlite из facts-прогонов + простой поиск (редакция, справки по ЖК).

Сборка:  docker compose run --rm --entrypoint python reader scripts/backlog_db.py build \
             --in backlog/facts_a.kept.jsonl --in backlog/facts_b.kept.jsonl
Поиск:   docker compose run --rm --entrypoint python reader scripts/backlog_db.py q \
             --text "название ЖК или девелопер" [--since 2024-01-01] [--limit 20]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

DB = Path("backlog/facts.db")


def _norm(s: str) -> str:
    """Нормализация для сравнения: lower, убрать лишние пробелы, унифицировать разделители."""
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = s.replace(",", ".").replace(" ", "")
    return s


def _fact_key(obj: str, fact: str) -> tuple:
    """Ключ схлопывания: (нормализованный объект, нормализованный факт)."""
    return (_norm(obj), _norm(fact))


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS facts(
        id INTEGER PRIMARY KEY, source TEXT, msg_id INTEGER, date TEXT,
        views INTEGER, obj TEXT, fact TEXT, snip TEXT)""")
    con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
        obj, fact, snip, content='facts', content_rowid='id')""")
    return con


def cmd_build(paths: list) -> None:
    # Читаем все kept-файлы, группируем по (obj, fact)
    groups: dict[tuple, dict] = {}
    total_rows = 0
    for p in paths:
        for ln in Path(p).read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            r = json.loads(ln)
            total_rows += 1
            for f in r.get("facts") or []:
                key = _fact_key(r.get("obj") or "", f)
                if key not in groups:
                    groups[key] = {
                        "obj": r.get("obj") or "",
                        "fact": f,
                        "snip": (r.get("text") or "")[:200],
                        "date": r.get("date") or "",
                        "views": int(r.get("views") or 0),
                        "sources": [],
                        "msg_ids": [],
                    }
                g = groups[key]
                src = r.get("source") or ""
                mid = r.get("msg_id")
                tag = f"{src}#{mid}" if src and mid else (src or str(mid or ""))
                if tag not in g["sources"]:
                    g["sources"].append(tag)
                if mid and mid not in g["msg_ids"]:
                    g["msg_ids"].append(mid)
                v = int(r.get("views") or 0)
                if v > g["views"]:
                    g["views"] = v
                if r.get("date") and (not g["date"] or r["date"] < g["date"]):
                    g["date"] = r["date"]
    print(f"строк до схлопывания: {total_rows} | уникальных фактов: {len(groups)}")

    # Пересоздаём БД
    if DB.exists():
        DB.unlink()
    con = _conn()
    n = 0
    for key, g in groups.items():
        sources_str = ", ".join(g["sources"])
        msg_ids_str = ",".join(str(x) for x in g["msg_ids"])
        cur = con.execute(
            "INSERT INTO facts(source, msg_id, date, views, obj, fact, snip) "
            "VALUES (?,?,?,?,?,?,?)",
            (sources_str, msg_ids_str, g["date"], g["views"],
             g["obj"], g["fact"], g["snip"]))
        con.execute("INSERT INTO facts_fts(rowid, obj, fact, snip) VALUES (?,?,?,?)",
                    (cur.lastrowid, g["obj"], g["fact"], g["snip"]))
        n += 1
    con.commit()
    print(f"фактов в базе: {n}")


def cmd_q(text: str, since: str, limit: int) -> None:
    con = _conn()
    q = """SELECT f.date, f.source, f.obj, f.fact, f.msg_id, f.views
           FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
           WHERE facts_fts MATCH ?"""
    args = [text]
    if since:
        q += " AND f.date >= ?"
        args.append(since)
    q += " ORDER BY f.date DESC LIMIT ?"
    args.append(limit)
    try:
        rows = con.execute(q, args).fetchall()
    except sqlite3.OperationalError:
        like = f"%{text}%"
        rows = con.execute(
            "SELECT date, source, obj, fact, msg_id, views FROM facts "
            "WHERE obj LIKE ? OR fact LIKE ? OR snip LIKE ? ORDER BY date DESC LIMIT ?",
            (like, like, like, limit)).fetchall()
    for d, src, obj, fact, mid, views in rows:
        print(f"{d[:10]} | {obj or '—'} | {fact} | sources: {src} | msg {mid} | views {views}")
    if not rows:
        print("ничего не найдено")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--in", dest="ins", action="append", required=True)
    q = sub.add_parser("q")
    q.add_argument("--text", required=True)
    q.add_argument("--since", default="")
    q.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()
    if a.cmd == "build":
        cmd_build(a.ins)
    else:
        cmd_q(a.text, a.since, a.limit)