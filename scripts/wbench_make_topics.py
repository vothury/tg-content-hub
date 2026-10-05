"""Нарезка боевых тем редакции в файлы бенча писателя.

Берёт последние темы главреда с материалами из БД (заголовки + тексты tg-постов)
и складывает topic_real_*.json в scripts/samples_writer/ (каталог монтируется rw).
Запуск: docker compose run --rm -v ./scripts/samples_writer:/app/scripts/samples_writer \
          --entrypoint python reader scripts/wbench_make_topics.py --limit 6
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from app.db.models import Headline, Post, Topic
from app.db.session import session_scope

OUT = Path(__file__).resolve().parent / "samples_writer"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    async with session_scope() as session:
        topics = (await session.execute(
            select(Topic).order_by(Topic.id.desc()).limit(args.limit))).scalars().all()
        for t in topics:
            ids = [int(x) for x in (t.materials or []) if str(x).strip().isdigit()]
            if not ids:
                continue
            heads = (await session.execute(
                select(Headline).where(Headline.id.in_(ids)))).scalars().all()
            texts = {}
            pids = [h.post_id for h in heads if h.post_id]
            if pids:
                rows = (await session.execute(
                    select(Post.id, Post.original_text).where(Post.id.in_(pids)))).all()
                texts = {pid: (txt or "")[:1500] for pid, txt in rows}
            materials = []
            for h in heads:
                body = texts.get(h.post_id)
                if h.source_kind == "tg" and body:
                    materials.append({"label": f"tg {h.source_name or ''} {h.fetched_at:%d.%m.%Y}",
                                      "text": f"{h.title}\n{body}"})
                else:
                    materials.append({"label": f"web {h.source_name or ''} {h.fetched_at:%d.%m.%Y}",
                                      "text": h.title + (f"\nurl: {h.url}" if h.url else "")})
            if not materials:
                continue
            (OUT / f"topic_real_{t.id}.json").write_text(json.dumps({
                "theme": t.theme, "hypothesis": t.hypothesis or "",
                "kind": t.kind.value, "kb": "", "expect": "write",
                "materials": materials}, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"topic_real_{t.id}.json: {len(materials)} материалов | {t.theme[:50]}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())