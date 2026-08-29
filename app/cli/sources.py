"""Управление источниками из командной строки (до админки из Этапа 6).

Примеры:
  make source-add USERNAME=@my_test_lab KIND=test
  make source-add USERNAME=@some_public_channel KIND=external
  make source-list
  make source-disable USERNAME=@some_public_channel
  make source-delete USERNAME=@some_public_channel          # только источник
  make source-delete USERNAME=@some_public_channel CASCADE  # источник + его посты/медиа
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import delete as sa_delete, select

from app.db.enums import SourceKind
from app.db.models import Post, Source
from app.db.session import session_scope


def _norm(username: str) -> str:
    return username.lstrip("@").strip()


async def add(username: str, kind: SourceKind) -> None:
    async with session_scope() as session:
        existing = (
            await session.execute(select(Source).where(Source.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Уже существует: #{existing.id} @{existing.username} ({existing.kind.value}, enabled={existing.enabled})")
            return
        source = Source(kind=kind, title=username, username=username)
        session.add(source)
        await session.commit()
        await session.refresh(source)
        print(f"Добавлен источник #{source.id}: @{username} ({kind.value})")


async def list_sources() -> None:
    async with session_scope() as session:
        rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
        if not rows:
            print("Источников нет. Добавьте: make source-add USERNAME=@канал KIND=test|external")
            return
        for r in rows:
            print(
                f"#{r.id} @{r.username} kind={r.kind.value} enabled={r.enabled} "
                f"tg_id={r.telegram_id} last_msg={r.last_read_message_id} title={r.title!r}"
            )


async def set_enabled(username: str, enabled: bool) -> None:
    async with session_scope() as session:
        source = (
            await session.execute(select(Source).where(Source.username == username))
        ).scalar_one_or_none()
        if source is None:
            print(f"Источник @{username} не найден")
            return
        source.enabled = enabled
        await session.commit()
        print(f"Источник #{source.id} @{username}: enabled={enabled}")


async def delete_source(username: str, cascade: bool) -> None:
    async with session_scope() as session:
        source = (
            await session.execute(select(Source).where(Source.username == username))
        ).scalar_one_or_none()
        if source is None:
            print(f"Источник @{username} не найден")
            return
        sid = source.id
        if cascade:
            # Каскад по FK удалит media_items и post_events автоматически
            deleted_posts = (
                await session.execute(sa_delete(Post).where(Post.source_id == sid))
            ).rowcount
            await session.delete(source)
            await session.commit()
            print(f"Удалён источник #{sid} @{username} вместе с {deleted_posts} пост(ами)")
        else:
            await session.delete(source)
            await session.commit()
            print(f"Удалён источник #{sid} @{username} (посты остались в базе)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Управление источниками")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("username")
    p_add.add_argument("--kind", choices=["external", "test"], default="external")

    sub.add_parser("list")

    p_set = sub.add_parser("set-enabled")
    p_set.add_argument("username")
    p_set.add_argument("enabled", choices=["true", "false"])

    p_del = sub.add_parser("delete")
    p_del.add_argument("username")
    p_del.add_argument("mode", nargs="?", choices=["CASCADE"], default=None,
                       help="CASCADE — удалить также посты и медиа источника")

    args = parser.parse_args()
    if args.cmd == "add":
        asyncio.run(add(_norm(args.username), SourceKind(args.kind)))
    elif args.cmd == "list":
        asyncio.run(list_sources())
    elif args.cmd == "set-enabled":
        asyncio.run(set_enabled(_norm(args.username), args.enabled == "true"))
    elif args.cmd == "delete":
        asyncio.run(delete_source(_norm(args.username), cascade=(args.mode == "CASCADE")))


if __name__ == "__main__":
    main()