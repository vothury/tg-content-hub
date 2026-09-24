"""Очистка медиафайлов, которые больше не нужны.

Удаляет файлы постов в терминальных статусах (UNSUITABLE/REJECTED/DEDUPLICATED/FAILED)
старше N дней и файлы-сироты (нет ссылки в media_items, например временные *_45mb.mp4).

Запуск:
    docker compose run --rm --entrypoint python api scripts/purge_media.py            # dry-run
    docker compose run --rm --entrypoint python api scripts/purge_media.py --apply    # удалить
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from sqlalchemy import text

from app.db.session import session_scope
from app.services.publishing import _media_root

TERMINAL = ("UNSUITABLE", "REJECTED", "DEDUPLICATED", "FAILED")


async def _terminal_files(root, days: int, published_days: int = 0):
    cond = ("p.status::text = any(:st) "
            "and p.created_at < now() - make_interval(days => :d)")
    params = {"st": list(TERMINAL), "d": days}
    if published_days:
        cond += (" or (p.status::text = 'PUBLISHED' "
                 "and p.created_at < now() - make_interval(days => :pd))")
        params["pd"] = published_days
    async with session_scope() as session:
        rows = (await session.execute(text(
            "select m.id, m.local_path, m.preview_path from media_items m "
            "join posts p on p.id = m.post_id where " + cond), params)).all()
    paths, ids = set(), []
    for mid, local_path, preview_path in rows:
        ids.append(mid)
        for rel in (local_path, preview_path):
            if rel:
                paths.add(root / rel)
    return paths, ids


async def _known_rels() -> set:
    async with session_scope() as session:
        rows = (await session.execute(text(
            "select local_path from media_items where local_path is not null"))).all()
        rows += (await session.execute(text(
            "select preview_path from media_items where preview_path is not null"))).all()
    return {r[0] for r in rows}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3,
                        help="возраст поста в терминальном статусе (дней)")
    parser.add_argument("--apply", action="store_true", help="реально удалять (иначе dry-run)")
    parser.add_argument("--published-days", type=int, default=0,
                        help="также удалять медиа опубликованных постов старше N дней "
                             "(0 = не трогать; повторная публикация таких постов уйдёт без медиа)")
    args = parser.parse_args()

    root = _media_root()
    terminal, terminal_ids = await _terminal_files(root, args.days, args.published_days)
    known = await _known_rels()
    orphans = {p for p in root.rglob("*") if p.is_file() and str(p.relative_to(root)) not in known}
    targets = sorted(terminal | orphans)
    total = sum(p.stat().st_size for p in targets if p.exists())
    print(f"терминальных файлов: {len(terminal)}, сирот: {len(orphans)}, "
          f"итого к удалению: {len(targets)} ({total / 1048576:.1f} МБ)")
    for p in targets[:20]:
        print("  -", p.relative_to(root), f"{p.stat().st_size / 1048576:.1f} МБ")
    if len(targets) > 20:
        print(f"  … и ещё {len(targets) - 20}")
    if not args.apply:
        print("dry-run: добавьте --apply для удаления")
        return
    removed, freed = 0, 0
    for p in targets:
        try:
            if p.exists():
                freed += p.stat().st_size
                p.unlink()
                removed += 1
        except OSError as exc:
            print("не удалось удалить", p, exc)
    for d in sorted({p.parent for p in targets}, key=lambda x: len(x.parts), reverse=True):
        try:
            if d != root and d.exists() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    if terminal_ids:
        async with session_scope() as session:
            await session.execute(text(
                "update media_items set downloaded=false, local_path=null, preview_path=null, "
                "phash=null, luma_mean=null where id = any(:ids)"), {"ids": terminal_ids})
            await session.commit()
        print(f"обнулены ссылки на удалённые файлы у media_items: {len(terminal_ids)} "
              "(перезапуск поста теперь перескачает медиа)")
    print(f"удалено файлов: {removed}, освобождено: {freed / 1048576:.1f} МБ")


asyncio.run(main())