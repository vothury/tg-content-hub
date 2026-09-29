"""Разовый просмотр всей истории источника (backlog) пакетной оценкой LLM.

Фаза 1: щадяще тянет историю канала батчами по 100 сообщений (пауза, FloodWait,
курсор для возобновления) в backlog_<source>_raw.jsonl.
Фаза 2: режет записи на батчи по N, оценивает через BACKLOG_SCAN, копит keep-ы.
Фаза 3: пишет таблицу Markdown (score, msg id, дата, подпись) + jsonl.

Запуск В КОНТЕЙНЕРЕ reader (там Telethon-сессия); reader на это время остановите,
чтобы не было двух клиентов на одной сессии:
  docker compose stop reader
  docker compose run --rm --entrypoint python reader scripts/backlog_scan.py \
      --source istoria --batch 30 --min-score 7 --pause 2.5 --tg-sleep 1.5 \
      --out backlog_istoria.md
  docker compose start reader
Повторный запуск с тем же --out возобновляется с курсора/батча.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # корень репо: импорт app.*

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from app.config import settings
from app.db.models import Post, Source
from app.db.session import session_scope
from app.services.llm.prompts import (
    BACKLOG_SCAN_SYSTEM, BACKLOG_SCAN_USER, BACKLOG_VERSION)
from app.services.llm.schemas import BacklogScanResult
from app.services.llm_pipeline import _call_with_fallback
from app.services.settings import Keys, get_providers, get_setting
from app.services.times import owner_tz

log = logging.getLogger("backlog_scan")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _paths(out: str):
    p = Path(out)
    return p, p.with_suffix(".jsonl"), p.with_suffix(".state.json")


def _load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


async def _fetch_history(client, entity, raw_path: Path, state: dict,
                         tg_sleep: float, limit: int = 0) -> int:
    """Фаза 1: история батчами по 100, пауза и FloodWait-бэк-офф, курсор в state."""
    added = 0
    cursor = state.get("cursor")
    seen = set()
    if raw_path.exists():
        for ln in raw_path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                seen.add(json.loads(ln)["id"])
    with raw_path.open("a", encoding="utf-8") as fh:
        while True:
            if limit and added >= limit:
                break
            kwargs = {"limit": 100}
            if cursor:
                kwargs["max_id"] = cursor
            try:
                msgs = await client.get_messages(entity, **kwargs)
            except FloodWaitError as exc:
                delay = min(int(getattr(exc, "seconds", 60)) + 5, 600)
                log.warning("FloodWait %s сек — пауза (курсор %s)", delay, cursor)
                await asyncio.sleep(delay)
                continue
            if not msgs:
                break
            if limit:
                msgs = msgs[:max(0, limit - added)]
            for m in msgs:
                if m.id in seen:
                    continue
                text = (getattr(m, "message", None) or "").strip()
                gid = getattr(m, "grouped_id", None)
                if not text and gid is None and getattr(m, "media", None) is None:
                    continue  # служебные сообщения
                fh.write(json.dumps({
                    "id": m.id,
                    "date": m.date.astimezone(timezone.utc).isoformat(),
                    "text": text,
                    "grouped_id": gid,
                }, ensure_ascii=False) + "\n")
                seen.add(m.id)
                added += 1
            fh.flush()
            cursor = min(m.id for m in msgs)
            state["cursor"] = cursor
            _save_state(raw_path.with_suffix(".state.json"), state)
            log.info("история: получено %d, курсор %s", added, cursor)
            await asyncio.sleep(tg_sleep)
    return added


def _save_state(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _entries(raw_path: Path) -> list:
    """Схлопывает альбомы (grouped_id) в одну запись: текст первого с текстом."""
    by_gid: dict = {}
    solo: list = []
    for ln in raw_path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        gid = r.get("grouped_id")
        if gid:
            e = by_gid.setdefault(gid, {"id": r["id"], "date": r["date"], "text": ""})
            if r["text"] and not e["text"]:
                e["text"] = r["text"]
            e["id"] = min(e["id"], r["id"])
        else:
            solo.append({"id": r["id"], "date": r["date"], "text": r["text"]})
    out = solo + list(by_gid.values())
    out = [e for e in out if (e["text"] or "").strip()]
    out.sort(key=lambda e: e["id"])
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--batch", type=int, default=30)
    ap.add_argument("--min-score", type=float, default=7.0)
    ap.add_argument("--pause", type=float, default=2.5, help="пауза между LLM-вызовами, сек")
    ap.add_argument("--tg-sleep", type=float, default=1.5, help="пауза между запросами истории, сек")
    ap.add_argument("--model", default="",
                    help="пусто = глобальная цепочка llm.classify_model; провайдеры каждой "
                         "модели — в скобках рядом с ней: 'slug (prov/quant, prov/quant), "
                         "slug2 (prov/quant)' (тот же синтаксис, что у classify_model в "
                         "sources.yaml); модели без скобок берут глобальные "
                         "llm.classify_providers")
    ap.add_argument("--out", default="media/backlog/backlog_scan.md")
    ap.add_argument("--limit", type=int, default=0,
                    help="тест: тянуть не больше N сообщений истории (0 = всю)")
    ap.add_argument("--max-batches", type=int, default=0,
                    help="тест: оценить не больше M батчей (0 = все)")
    args = ap.parse_args()
    if args.batch < 1:
        raise SystemExit("--batch должен быть >= 1")
    if not 0 <= args.min_score <= 10:
        raise SystemExit("--min-score в пределах 0..10")
    args.pause = max(0.0, args.pause)
    args.tg_sleep = max(0.0, args.tg_sleep)

    out_path, raw_path, state_path = _paths(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)   # media/backlog/ и т.п. создаём сами
    state = _load_state(state_path)

    client = TelegramClient(settings.reader_session_path,
                            settings.telegram_api_id, settings.telegram_api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("сессия не авторизована")
    try:
        entity = await client.get_entity(args.source)
        added = await _fetch_history(client, entity, raw_path, state, args.tg_sleep,
                                     limit=args.limit)
        log.info("фаза 1 завершена: новых сообщений %d", added)

        async with session_scope() as s:
            providers = await get_providers(s, Keys.CLASSIFY_PROVIDERS)
            model = args.model or str(await get_setting(s, Keys.CLASSIFY_MODEL))
            src_id = (await s.execute(
                __import__("sqlalchemy").select(Source.id)
                .where(Source.username == args.source))).scalar_one_or_none()
            known = set()
            if src_id is not None:
                known = set(x for x in (await s.execute(
                    __import__("sqlalchemy").select(Post.source_message_id)
                    .where(Post.source_id == src_id))).scalars() if x)

        entries = [e for e in _entries(raw_path) if e["id"] not in known]
        log.info("фаза 2: записей к оценке %d (известных БД пропущено %d)",
                 len(entries), len(known))
        kept: list = state.setdefault("kept", [])
        done_batches = int(state.get("done_batches", 0))
        cost_total = float(state.get("cost", 0.0))
        total = (len(entries) + args.batch - 1) // args.batch
        if args.max_batches:
            total = min(total, args.max_batches)
        for bi in range(done_batches, total):
            chunk = entries[bi * args.batch:(bi + 1) * args.batch]
            listing = "\n".join(
                f"{n}. {(e['text'] or '')[:300]}" for n, e in enumerate(chunk, 1))
            messages = [
                {"role": "system", "content": BACKLOG_SCAN_SYSTEM},
                {"role": "user", "content": BACKLOG_SCAN_USER.format(listing=listing)},
            ]
            result = None
            for attempt in (1, 2):
                used, resp, result, status, error, rotation = await _call_with_fallback(
                    messages, model, 900, 0.1, BacklogScanResult,
                    providers, settings.llm_reasoning_small)
                if resp is not None and resp.cost_usd:
                    cost_total += float(resp.cost_usd)
                if result is not None:
                    break
                log.warning("батч %d/%d попытка %d не дала результата: %s",
                            bi + 1, total, attempt, (error or "")[:120])
                await asyncio.sleep(30)
            if result is not None:
                by_i = {n: e for n, e in enumerate(chunk, 1)}
                for it in result.items:
                    e = by_i.get(it["i"])
                    if e is None or not it["keep"] or it["score"] < args.min_score:
                        continue
                    kept.append({
                        "msg_id": e["id"], "date": e["date"],
                        "score": it["score"],
                        "caption": it["caption"] or (e["text"] or "")[:110],
                        "batch": bi + 1,
                    })
            state["done_batches"] = bi + 1
            state["cost"] = cost_total
            state["kept"] = kept
            _save_state(state_path, state)
            if (bi + 1) % 10 == 0:
                log.info("батч %d/%d, keep=%d, стоимость $%.4f",
                         bi + 1, total, len(kept), cost_total)
            await asyncio.sleep(args.pause)

        seen_ids: set = set()
        uniq: list = []
        for r in kept:
            if r["msg_id"] in seen_ids:
                continue
            seen_ids.add(r["msg_id"])
            uniq.append(r)
        kept = uniq
        state["kept"] = kept
        kept.sort(key=lambda r: (-r["score"], r["date"]))
        tz = owner_tz()
        lines = [f"# Backlog {args.source}: отобрано {len(kept)} "
                 f"(порог {args.min_score}, {BACKLOG_VERSION}, $ {cost_total:.4f})",
                 "", "| score | msg id | дата | подпись |", "|---|---|---|---|"]
        for r in kept:
            from datetime import datetime as _dt
            d = _dt.fromisoformat(r["date"]).astimezone(tz).strftime("%d.%m.%Y")
            cap = r["caption"].replace("|", "/").replace("\n", " ")
            lines.append(f"| {r['score']:.0f} | {r['msg_id']} | {d} | {cap} |")
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        raw_path.with_suffix(".kept.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
            encoding="utf-8")
        log.info("готово: %s строк в %s, стоимость $%.4f", len(kept), out_path, cost_total)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())