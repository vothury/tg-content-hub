"""Одноразовая утилизация: опубликованные посты -> webp-превью вместо тяжёлых оригиналов."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import select

from app.common.logging import setup_logging
from app.config import settings
from app.db.enums import MediaType, PostStatus
from app.db.models import MediaItem, Post
from app.db.session import session_scope

log = setup_logging("backfill_previews")

MEDIA_ROOT = Path(settings.media_dir)
if not MEDIA_ROOT.is_absolute():
    MEDIA_ROOT = Path("/app") / settings.media_dir


def _dhash(img) -> int:
    g = img.convert("L").resize((9, 8))
    px = g.tobytes()
    h = 0
    for r in range(8):
        for c in range(8):
            if px[r * 9 + c] > px[r * 9 + c + 1]:
                h |= 1 << (r * 8 + c)
    if h >= 1 << 63:
        h -= 1 << 64
    return h


def _make_preview(local_path: Path, media_type) -> tuple[str | None, int | None]:
    try:
        from PIL import Image
        src = None
        if media_type is MediaType.VIDEO:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                src = tf.name
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(local_path),
                 "-vf", "select=eq(n\\,0)", "-vframes", "1", src],
                check=True, timeout=60,
            )
        else:
            src = str(local_path)
        with Image.open(src) as im:
            ph = _dhash(im)
            prev = im.convert("RGB")
            prev.thumbnail((480, 480))
            prev_dir = local_path.parent / "prev"
            prev_dir.mkdir(parents=True, exist_ok=True)
            prev_path = prev_dir / (local_path.stem + ".webp")
            prev.save(prev_path, "WEBP", quality=30)
        if src and src != str(local_path):
            os.unlink(src)
        return str(prev_path.resolve().relative_to(MEDIA_ROOT)), ph
    except Exception:  # noqa: BLE001
        log.exception("не удалось создать превью: %s", local_path)
        return None, None


async def main() -> None:
    async with session_scope() as session:
        rows = (await session.execute(
            select(MediaItem.id, MediaItem.local_path, MediaItem.media_type, MediaItem.phash)
            .select_from(MediaItem)
            .join(Post, Post.id == MediaItem.post_id)
            .where(Post.status == PostStatus.PUBLISHED,
                   MediaItem.local_path.isnot(None))
        )).all()
    log.info("к обработке: %d медиа опубликованных постов", len(rows))
    freed = 0
    done = 0
    for mid, rel, mtype, ph in rows:
        p = MEDIA_ROOT / rel
        if not p.exists():
            continue
        prev_rel, new_ph = _make_preview(p, mtype)
        size = p.stat().st_size
        async with session_scope() as session:
            m = await session.get(MediaItem, mid)
            if m is None:
                continue
            if prev_rel:
                m.preview_path = prev_rel
            if m.phash is None and new_ph is not None:
                m.phash = new_ph
            m.local_path = None
            m.downloaded = False
            await session.commit()
        try:
            p.unlink()
            freed += size
            done += 1
            for d in (p.parent, p.parent.parent):
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
        except Exception:  # noqa: BLE001
            log.warning("не удалось удалить оригинал %s", p)
    log.info("готово: удалено %d оригиналов, освобождено ~%.1f МБ", done, freed / 1e6)


if __name__ == "__main__":
    asyncio.run(main())