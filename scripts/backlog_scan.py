"""Разовый просмотр всей истории источника (backlog) пакетной оценкой LLM.

Фаза 1: щадяще тянет историю канала батчами по 100 сообщений (пауза, FloodWait,
курсор для возобновления, ограничение периода --since) в <out>.jsonl (raw) вместе
с полной метадатой: views, forwards, reply_count, реакции суммой и по эмодзи,
тип медиа, флаг правки.
Фаза 2: схлопывает альбомы, вычитает известные БД посты, пропускает через
регекс-префильтр, режет на батчи и оценивает одним из режимов:
  taste  - отбор постов по вкусу канала (таблица keep-ов);
  audit  - доли категорий (зонд источника, вердикт ДОПУСТИТЬ/ИСКЛЮЧИТЬ, ниша --niche);
  facts  - извлечение фактов (даты/объекты/числа) в базу знаний редакции.
Фаза 3: пишет отчёт Markdown + <out>.kept.jsonl; состояние (курсор, обработанные
id, стоимость, keep-ы) живёт в <out>.state.json - повторный запуск с тем же --out
возобновляется; необработанные батчи дооцениваются, упавшие не помечаются.

Защиты: бан модели после двух БАТЧЕЙ подряд с обрывом на лимите токенов
(страйк начисляется раз на батч, а не на попытку); аварийный стоп при полностью
забаненной цепочке; предупреждение о номерах вне списка (сбой нумерации модели).

Запуск В КОНТЕЙНЕРЕ reader (там Telethon-сессия); reader на время остановите:
  docker compose stop reader
  docker compose run --rm --entrypoint python reader scripts/backlog_scan.py \
      --source istoria --mode facts --since 2023-09-30 --batch 25 --reasoning 1500 \
      --prefilter "ЖК|новострой|застройщик|эскроу|ипотек|м²" \
      --pause 2.5 --tg-sleep 1.5 \
      --model "openai/gpt-oss-20b (darkbloom/fp8)" \
      --out backlog/facts_istoria.md
  docker compose start reader
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import re
import sys
from datetime import datetime as _dt, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # корень репо: импорт app.*

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from app.config import settings
from app.db.models import Post, Source
from app.db.session import session_scope
from app.services.llm.prompts import (
    BACKLOG_AUDIT_SYSTEM, BACKLOG_AUDIT_VERSION, BACKLOG_FACTS_SYSTEM,
    BACKLOG_FACTS_VERSION, BACKLOG_SCAN_SYSTEM, BACKLOG_SCAN_USER, BACKLOG_VERSION)
from app.services.llm.schemas import (
    BacklogAuditResult, BacklogFactsResult, BacklogScanResult)
from app.services.llm_pipeline import _call_with_fallback
from app.services.settings import Keys, get_providers, get_setting
from app.services.times import owner_tz

log = logging.getLogger("backlog_scan")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Обрыв на лимите токенов: рассуждения съели бюджет, финальный JSON не вышел.
_LIMIT_MARKS = ("лимит токенов исчерпан", "reasoning loop")

# Поддерживает ли текущий llm_pipeline исключение моделей из цепочки (бан).
_SUPPORTS_EXCLUDE = "exclude" in inspect.signature(_call_with_fallback).parameters

# «Объекты» без имени собственного: факт без субъекта - шум, в kept не пускаем.
_GENERIC_OBJ = {
    "жк", "рынок", "рынок москвы", "рынок недвижимости", "москва", "московский рынок",
    "столичный рынок", "столица", "объект", "проект", "компания", "застройщик",
    "дом", "квартиры", "новостройки", "недвижимость", "россия", "рф", "регион",
    "отрасль", "город",
}


def _is_limit_error(err: str) -> bool:
    e = (err or "").lower()
    if any(m in e for m in _LIMIT_MARKS):
        return True
    return "из ответа: ''" in e   # пустой финал - рассуждения съели весь бюджет


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


def _save_state(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


async def _fetch_history(client, entity, raw_path: Path, state: dict,
                         tg_sleep: float, limit: int = 0, since=None) -> int:
    """Фаза 1: история батчами по 100, пауза и FloodWait-бэк-офф, курсор в state.

    limit - максимум новых сообщений (0 = без лимита); since - не читать сообщения
    старше этой даты (datetime, UTC; None = без ограничения по периоду).
    """
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
                log.warning("FloodWait %s сек - пауза (курсор %s)", delay, cursor)
                await asyncio.sleep(delay)
                continue
            if not msgs:
                break
            # Период: история идёт от новых к старым; если вся пачка старше since - стоп.
            if since is not None:
                msgs = [m for m in msgs if m.date >= since]
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
                rc = getattr(m, "reactions", None)
                rlist = getattr(rc, "results", None) or (rc if isinstance(rc, list) else [])
                rmap, rsum = {}, 0
                for r in rlist:
                    cnt = int(getattr(r, "count", 0) or 0)
                    emo = getattr(getattr(r, "reaction", None), "emoticon", None) \
                        or str(getattr(r, "reaction", "") or "?")
                    rmap[emo] = rmap.get(emo, 0) + cnt
                    rsum += cnt
                media_kind = ("photo" if getattr(m, "photo", None)
                              else "video" if getattr(m, "video", None)
                              else "document" if getattr(m, "document", None)
                              else "other" if getattr(m, "media", None) else None)
                fh.write(json.dumps({
                    "id": m.id,
                    "date": m.date.astimezone(timezone.utc).isoformat(),
                    "text": text,
                    "grouped_id": gid,
                    "media": media_kind,
                    "views": getattr(m, "views", None),
                    "forwards": getattr(m, "forwards", None),
                    "reply_count": getattr(m, "reply_count", None),
                    "reactions": rsum,
                    "reactions_map": rmap,
                    "edited": bool(getattr(m, "edit_date", None)),
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


def _entries(raw_path: Path) -> list:
    """Схлопывает альбомы (grouped_id) в одну запись с переносом метаданных.

    Просмотры/форварды/комментарии - максимум по участникам альбома,
    реакции - сумма и поштучное сложение карты, медиа - первое непустое.
    Старые raw без новых полей совместимы (r.get вернёт None/{}).
    """
    by_gid: dict = {}
    solo: list = []
    for ln in raw_path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        gid = r.get("grouped_id")
        if gid:
            e = by_gid.setdefault(gid, {"id": r["id"], "date": r["date"], "text": "",
                                        "media": None,
                                        "views": r.get("views"), "forwards": r.get("forwards"),
                                        "reply_count": r.get("reply_count"),
                                        "reactions": 0, "reactions_map": {}})
            if r["text"] and not e["text"]:
                e["text"] = r["text"]
            e["id"] = min(e["id"], r["id"])
            if r.get("media") and not e["media"]:
                e["media"] = r["media"]
            e["views"] = max(e["views"] or 0, r.get("views") or 0) or None
            e["forwards"] = max(e["forwards"] or 0, r.get("forwards") or 0) or None
            e["reply_count"] = max(e["reply_count"] or 0, r.get("reply_count") or 0) or None
            e["reactions"] = (e["reactions"] or 0) + (r.get("reactions") or 0)
            for k, v in (r.get("reactions_map") or {}).items():
                e["reactions_map"][k] = e["reactions_map"].get(k, 0) + v
        else:
            solo.append({"id": r["id"], "date": r["date"], "text": r["text"],
                         "media": r.get("media"),
                         "views": r.get("views"), "forwards": r.get("forwards"),
                         "reply_count": r.get("reply_count"),
                         "reactions": r.get("reactions") or 0,
                         "reactions_map": r.get("reactions_map") or {}})
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
                         "модели - в скобках рядом с ней: 'slug (prov/quant, prov/quant), "
                         "slug2 (prov/quant)' (тот же синтаксис, что у classify_model в "
                         "sources.yaml); модели без скобок берут глобальные llm.classify_providers")
    ap.add_argument("--out", default="backlog/backlog_scan.md")
    ap.add_argument("--limit", type=int, default=0,
                    help="тест: тянуть не больше N сообщений истории (0 = всю)")
    ap.add_argument("--max-batches", type=int, default=0,
                    help="тест: оценить не больше M батчей (0 = все)")
    ap.add_argument("--mode", choices=("taste", "audit", "facts"), default="taste",
                    help="taste = отбор по вкусу канала; audit = доли категорий (зонд "
                         "источника); facts = извлечение фактов (числа/даты/объекты) в базу")
    ap.add_argument("--prefilter", default="",
                    help="регекс-гейт: записи без совпадения не попадают в батчи "
                         "(срежет объём бесплатно)")
    ap.add_argument("--since", default="",
                    help="не читать историю раньше даты YYYY-MM-DD (период опроса)")
    ap.add_argument("--niche", default="",
                    help="описание ниши для режима audit (по умолчанию - первичка Москвы/МО)")
    ap.add_argument("--reasoning", type=int, default=1000,
                    help="бюджет рассуждений вызовов скана (единый на обе попытки; "
                         "для facts рекомендуется 1500)")
    args = ap.parse_args()

    if args.batch < 1:
        raise SystemExit("--batch должен быть >= 1")
    if not 0 <= args.min_score <= 10:
        raise SystemExit("--min-score в пределах 0..10")
    args.pause = max(0.0, args.pause)
    args.tg_sleep = max(0.0, args.tg_sleep)
    since_dt = None
    if args.since:
        since_dt = _dt.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    out_path, raw_path, state_path = _paths(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)   # backlog/ и т.п. создаём сами
    state = _load_state(state_path)
    if not _SUPPORTS_EXCLUDE:
        log.info("llm_pipeline без параметра exclude: бан моделей будет только логироваться; "
                 "примените патч _call_with_fallback, чтобы бан фильтровал цепочку")

    client = TelegramClient(settings.reader_session_path,
                            settings.telegram_api_id, settings.telegram_api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("сессия не авторизована")
    try:
        entity = await client.get_entity(args.source)
        added = await _fetch_history(client, entity, raw_path, state, args.tg_sleep,
                                     limit=args.limit, since=since_dt)
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
        if args.prefilter:
            pre_re = re.compile(args.prefilter, re.I)
            before = len(entries)
            entries = [e for e in entries if pre_re.search(e["text"] or "")]
            log.info("префильтр: %d -> %d записей", before, len(entries))
        log.info("фаза 2: записей к оценке %d (известных БД пропущено %d)",
                 len(entries), len(known))

        kept: list = state.setdefault("kept", [])
        done_ids: set = set(state.setdefault("done_ids", []))
        cost_total = float(state.get("cost", 0.0))
        banned: set = set()
        strikes: dict = {}
        pending = [e for e in entries if e["id"] not in done_ids]
        total = (len(pending) + args.batch - 1) // args.batch
        if args.max_batches:
            total = min(total, args.max_batches)
        niche_default = ("Moscow/region PRIMARY market - residential complexes, developers, "
                         "prices per m2, mortgages, construction stages, permits, renovation/KRT")
        for bi in range(total):
            chunk = pending[bi * args.batch:(bi + 1) * args.batch]
            cut = {"taste": 300, "audit": 300, "facts": 700}[args.mode]
            listing = "\n".join(
                (f"{n}. [{_dt.fromisoformat(e['date']).strftime('%d.%m.%Y')}] "
                 f"{(e['text'] or '')[:cut]}") if args.mode == "facts"
                else f"{n}. {(e['text'] or '')[:cut]}"
                for n, e in enumerate(chunk, 1))
            system = {"taste": BACKLOG_SCAN_SYSTEM,
                      "audit": BACKLOG_AUDIT_SYSTEM.format(niche=args.niche or niche_default),
                      "facts": BACKLOG_FACTS_SYSTEM}[args.mode]
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": BACKLOG_SCAN_USER.format(listing=listing)},
            ]
            schema = {"taste": BacklogScanResult,
                      "audit": BacklogAuditResult,
                      "facts": BacklogFactsResult}[args.mode]
            out_tokens = {"taste": 900, "audit": 600, "facts": 1200}[args.mode]
            result = None
            batch_limit_fails: set = set()
            batch_ok: set = set()
            for attempt in (1, 2):
                call_kwargs = {"exclude": banned} if _SUPPORTS_EXCLUDE else {}
                used, resp, result, status, error, rotation = await _call_with_fallback(
                    messages, model, out_tokens, 0.1, schema,
                    providers, args.reasoning, **call_kwargs)
                if resp is not None and resp.cost_usd:
                    cost_total += float(resp.cost_usd)
                for r in rotation:
                    slug = r.get("model")
                    if _is_limit_error(r.get("error")):
                        batch_limit_fails.add(slug)
                    else:
                        batch_ok.add(slug)
                if result is not None:
                    batch_ok.add(used)
                    break
                log.warning("батч %d/%d попытка %d не дала результата: %s",
                            bi + 1, total, attempt, (error or "")[:120])
                await asyncio.sleep(30)
            # Пустая цепочка (все модели забанены) = нет ни вызовов, ни rotation:
            # не крутим батчи впустую, останавливаем прогон чисто.
            if resp is None and not rotation:
                log.critical("цепочка пуста: все модели забанены (%s) - прогон остановлен; "
                             "возобновите той же командой позже, батчи не потеряны",
                             sorted(banned))
                break
            # Страйк начисляется ОДИН раз на батч: две попытки одного батча не должны
            # банить всю цепочку разом; успех модели в батче обнуляет её страйк.
            for slug in batch_limit_fails - batch_ok:
                strikes[slug] = strikes.get(slug, 0) + 1
                if strikes[slug] >= 2 and slug not in banned:
                    banned.add(slug)
                    log.warning("модель %s исключена из цепочки скана: "
                                "2 батча подряд с обрывом на лимите токенов", slug)
                    if not _SUPPORTS_EXCLUDE:
                        log.warning("бан не фильтрует цепочку: примените патч "
                                    "exclude в app/services/llm_pipeline.py")
            for slug in batch_ok:
                strikes[slug] = 0
            if result is not None:
                by_i = {n: e for n, e in enumerate(chunk, 1)}
                unknown_i = 0
                for it in result.items:
                    e = by_i.get(it["i"])
                    if e is None:
                        unknown_i += 1
                        continue
                    if args.mode == "taste":
                        if not it["keep"] or it["score"] < args.min_score:
                            continue
                        kept.append({"msg_id": e["id"], "date": e["date"],
                                     "score": it["score"],
                                     "caption": it["caption"] or (e["text"] or "")[:110],
                                     "views": e.get("views"), "forwards": e.get("forwards"),
                                     "replies": e.get("reply_count"),
                                     "reactions": e.get("reactions"),
                                     "reactions_map": e.get("reactions_map"),
                                     "media": e.get("media"), "batch": bi + 1})
                    elif args.mode == "audit":
                        kept.append({"msg_id": e["id"], "date": e["date"], "cat": it["cat"],
                                     "views": e.get("views"), "forwards": e.get("forwards"),
                                     "replies": e.get("reply_count"),
                                     "reactions": e.get("reactions"),
                                     "reactions_map": e.get("reactions_map"),
                                     "media": e.get("media"), "batch": bi + 1})
                    else:
                        if not it["rel"] or not it["facts"]:
                            continue
                        obj = (it["obj"] or "").strip().strip('«»"')
                        if not obj or obj.lower() in _GENERIC_OBJ:
                            continue   # безымянный «объект» - в базу не пишем
                        kept.append({"msg_id": e["id"], "date": e["date"], "obj": obj,
                                     "facts": it["facts"], "views": e.get("views"),
                                     "forwards": e.get("forwards"),
                                     "replies": e.get("reply_count"),
                                     "reactions": e.get("reactions"),
                                     "reactions_map": e.get("reactions_map"),
                                     "media": e.get("media"),
                                     "text": (e["text"] or "")[:200],
                                     "source": args.source, "batch": bi + 1})
                if unknown_i:
                    log.warning("батч %d/%d: %d элемент(ов) с номером вне списка - "
                                "модель сбила нумерацию, сверьте kept с raw",
                                bi + 1, total, unknown_i)
                # Помечаем обработанными ТОЛЬКО успешные батчи: упавшие дооценит resume.
                done_ids.update(e["id"] for e in chunk)
                state["done_ids"] = sorted(done_ids)
            state["cost"] = cost_total
            state["kept"] = kept
            _save_state(state_path, state)
            if (bi + 1) % 10 == 0:
                log.info("батч %d/%d, keep=%d, стоимость $%.4f%s",
                         bi + 1, total, len(kept), cost_total,
                         f", исключены: {sorted(banned)}" if banned else "")
            await asyncio.sleep(args.pause)

        seen_ids = set()
        kept = [r for r in kept if not (r["msg_id"] in seen_ids or seen_ids.add(r["msg_id"]))]
        state["kept"] = kept
        tz = owner_tz()
        if args.mode == "taste":
            kept.sort(key=lambda r: (-r["score"], r["date"]))
            lines = [f"# Backlog {args.source}: отобрано {len(kept)} "
                     f"(порог {args.min_score}, {BACKLOG_VERSION}, $ {cost_total:.4f})",
                     "", "| score | msg id | дата | подпись |", "|---|---|---|---|"]
            for r in kept:
                d = _dt.fromisoformat(r["date"]).astimezone(tz).strftime("%d.%m.%Y")
                cap = r["caption"].replace("|", "/").replace("\n", " ")
                lines.append(f"| {r['score']:.0f} | {r['msg_id']} | {d} | {cap} |")
        elif args.mode == "audit":
            from collections import Counter
            cnt = Counter(r["cat"] for r in kept)
            total_n = max(1, sum(cnt.values()))
            years: dict = {}
            for r in kept:
                years.setdefault(r["date"][:4], Counter())[r["cat"]] += 1
            profile_n = cnt.get("profile", 0)
            share = profile_n / total_n
            # окно аудита в месяцах (по фактическому диапазону дат) -> экстраполяция на 3 года
            ds = sorted(r["date"] for r in kept)
            if len(ds) >= 2:
                months = max(1, round((_dt.fromisoformat(ds[-1])
                                       - _dt.fromisoformat(ds[0])).days / 30.44))
            elif since_dt is not None:
                months = max(1, round((_dt.now(timezone.utc) - since_dt).days / 30.44))
            else:
                months = 1
            per_month = profile_n / months
            est_3y = int(per_month * 36)
            verdict = ("ДОПУСТИТЬ к глубокому скану"
                       if (share >= 0.20 or est_3y >= 500) else "ИСКЛЮЧИТЬ")
            lines = [f"# Аудит {args.source}: {total_n} постов, {BACKLOG_AUDIT_VERSION}, $ {cost_total:.4f}",
                     f"# Вердикт: {verdict} (порог: profile >= 20% ИЛИ >= 500 профильных за 3 года)",
                     f"# Профиль: {profile_n} ({share:.1%}) за {months} мес окна; "
                     f"~{per_month:.0f}/мес; ~{est_3y} за 3 года", "",
                     "| категория | кол-во | доля |", "|---|---|---|"]
            for cat, n in cnt.most_common():
                lines.append(f"| {cat} | {n} | {n / total_n:.1%} |")
            lines += ["", "| год | доля profile |", "|---|---|"]
            for y in sorted(years):
                yc = years[y]
                lines.append(f"| {y} | {yc.get('profile', 0) / max(1, sum(yc.values())):.1%} |")
        else:
            kept.sort(key=lambda r: r["date"])
            lines = [f"# Факты {args.source}: {len(kept)} постов с фактами "
                     f"({BACKLOG_FACTS_VERSION}, $ {cost_total:.4f})", "",
                     "| дата | msg id | объект | факты |", "|---|---|---|---|"]
            for r in kept:
                d = _dt.fromisoformat(r["date"]).astimezone(tz).strftime("%d.%m.%Y")
                facts = "; ".join(r["facts"]).replace("|", "/")
                lines.append(f"| {d} | {r['msg_id']} | {r['obj'] or '—'} | {facts} |")
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        raw_path.with_suffix(".kept.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
            encoding="utf-8")
        log.info("готово: %s строк в %s, стоимость $%.4f%s",
                 len(kept), out_path, cost_total,
                 f", исключены модели: {sorted(banned)}" if banned else "")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())