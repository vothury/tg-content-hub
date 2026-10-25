"""Датасет медиа-гейта: манифест вердиктов + подборка картинок для отсмотра владельцем.

Расхождения моделей обязательны в подборке, согласные — выборочно (до 10).
Выход: backlog/media_dataset/manifest_<дата>.jsonl и img/<media_id>.<ext>.
Разметка владельца: labels.csv (media_id,relevant,watermark) -> вход бенча M2.

  docker compose run --rm -v ./backlog/media_dataset:/app/backlog/media_dataset \
    --entrypoint python reader scripts/media_dataset.py --days 4
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.db.models import MediaItem, Post, PostEvent
from app.db.session import session_scope


async def build(days: int) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with session_scope() as session:
        events = (await session.execute(
            select(PostEvent).where(PostEvent.action == "media_verdicts",
                                    PostEvent.created_at >= since)
            .order_by(PostEvent.id))).scalars().all()
        rows = []
        for e in events:
            p = await session.get(Post, e.post_id)
            title = (p.draft_text or p.original_text or "").split("\n", 1)[0][:80]
            for it in (e.details or {}).get("items") or []:
                rows.append({"media_id": it["media_id"], "path": it["path"],
                             "post_id": e.post_id, "title": title,
                             "main": it.get("main"), "fb": it.get("fb"),
                             "agree": it.get("agree")})
    out = Path("backlog/media_dataset")
    (out / "img").mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with (out / f"manifest_{stamp}.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    disagree = [r for r in rows if r["agree"] is False]
    agree = [r for r in rows if r["agree"] is not False]
    pick = disagree + random.sample(agree, min(10, len(agree)))
    copied = 0
    for r in pick:
        src = Path(r["path"] or "")
        if not src.is_absolute():
            src = Path("/app") / src
        if src.exists():
            shutil.copy2(src, out / "img" / f"{r['media_id']}{src.suffix}")
            copied += 1
    print(f"строк манифеста: {len(rows)}, расхождений: {len(disagree)}, "
          f"скопировано картинок: {copied}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=4)
    args = ap.parse_args()
    asyncio.run(build(args.days))


if __name__ == "__main__":
    main()