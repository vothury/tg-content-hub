"""База знаний backlog: sqlite из facts-прогонов + простой поиск (редакция, справки по ЖК).

Сборка:  docker compose run --rm --entrypoint python reader scripts/backlog_db.py build \
             --in backlog/facts_a.kept.jsonl --in backlog/facts_b.kept.jsonl
Поиск:   docker compose run --rm --entrypoint python reader scripts/backlog_db.py q \
             --text "название ЖК или девелопер" [--since 2024-01-01] [--limit 20]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DB = Path("backlog/facts.db")


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
    con = _conn()
    n = 0
    for p in paths:
        for ln in Path(p).read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            r = json.loads(ln)
            for f in r.get("facts") or []:
                cur = con.execute(
                    "INSERT INTO facts(source, msg_id, date, views, obj, fact, snip) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (r.get("source"), r.get("msg_id"), r.get("date"),
                     r.get("views"), r.get("obj"), f, r.get("text", "")[:200]))
                con.execute("INSERT INTO facts_fts(rowid, obj, fact, snip) VALUES (?,?,?,?)",
                            (cur.lastrowid, r.get("obj") or "", f, r.get("text", "")[:200]))
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
        print(f"{d[:10]} | {src} | {obj or '—'} | {fact} | msg {mid} | views {views}")
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