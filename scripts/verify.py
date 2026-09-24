"""Автоматические проверки после деплоя.

Запуск:
    docker compose run --rm --entrypoint python api scripts/verify.py
    docker compose run --rm --entrypoint python api scripts/verify.py --net --llm --write

Флаги:
    --net    разрешить внешние HTTP-запросы (каталог OpenRouter)
    --llm    разрешить один дешёвый вызов модели (семантика дедупликации)
    --write  разрешить запись (прогон watch_models: история цен)
    --json   вывести результат в JSON (для скриптов/CI)

Код возврата: 0 — FAIL нет, 1 — есть хотя бы один FAIL.
Проверки соответствуют разделам docs/verification.md (A, B, C, D, E, F, G, H, I, J, K).
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import shutil
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from app.db.session import session_scope

OK, WARN, FAIL = "OK", "WARN", "FAIL"
RESULTS: list = []
FLAGS = argparse.Namespace(net=False, llm=False, write=False, json=False)


async def q1(sql: str, **params):
    async with session_scope() as session:
        return (await session.execute(text(sql), params)).scalar()


async def qall(sql: str, **params):
    async with session_scope() as session:
        return (await session.execute(text(sql), params)).all()


# ---------------------------------------------------------------- A. Smoke

async def chk_alembic():
    db = await q1("select version_num from alembic_version")
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        heads = ScriptDirectory.from_config(Config("/app/alembic.ini")).get_heads()
    except Exception as exc:
        return WARN, f"БД {db}; heads не определены ({exc.__class__.__name__})"
    if len(heads) == 1 and heads[0] == db:
        return OK, f"{db}"
    return FAIL, f"БД {db} != heads {heads}"


MODULES = (
    "app.web.main", "app.services.llm_pipeline", "app.services.publishing",
    "app.services.review", "app.services.price_watch", "app.services.dedup",
    "app.services.prefilter", "app.services.security", "app.services.queue",
    "app.workers.reader", "app.workers.scheduler", "app.services.editorial_journalist",
)


async def chk_imports():
    bad = []
    for m in MODULES:
        try:
            importlib.import_module(m)
        except Exception as exc:
            bad.append(f"{m}: {exc.__class__.__name__}")
    return (FAIL, "; ".join(bad)) if bad else (OK, f"{len(MODULES)} модулей")


async def chk_redis():
    from app.redis_client import get_redis
    pong = await get_redis().ping()
    return (OK if pong else FAIL), "PONG" if pong else "нет ответа"


async def chk_price_stamp():
    from app.redis_client import get_redis
    v = await get_redis().get("price_watch:last_run")
    if isinstance(v, bytes):
        v = v.decode("utf-8", "ignore")
    if not v:
        return WARN, "отметки нет — цикл price_watch не запускался?"
    try:
        ts = datetime.fromisoformat(str(v).split(" ")[0])
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return WARN, f"не разобрал отметку: {v}"
    return (OK if age_h <= 26 else WARN), f"{v} (возраст {age_h:.1f} ч)"


async def chk_disk():
    free_gb = shutil.disk_usage("/").free / 1024 ** 3
    media = "?"
    try:
        from app.services.publishing import _media_root
        media = str(_media_root())
    except Exception:
        pass
    if free_gb < 0.5:
        return FAIL, f"свободно {free_gb:.2f} ГБ (<0.5); media: {media}"
    if free_gb < 1.5:
        return WARN, f"свободно {free_gb:.2f} ГБ (<1.5); media: {media}"
    return OK, f"свободно {free_gb:.2f} ГБ; media: {media}"


async def chk_mem():
    mb = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    mb = int(line.split()[1]) / 1024
                    break
    except Exception as exc:
        return WARN, f"/proc/meminfo недоступен: {exc.__class__.__name__}"
    if mb is None:
        return WARN, "MemAvailable не найден"
    if mb < 300:
        return WARN, f"доступно {mb:.0f} МБ (<300)"
    return OK, f"доступно {mb:.0f} МБ"


# ------------------------------------------------------- B. Price watch

async def chk_price_targets():
    from app.services.price_watch import _watched_targets
    targets = await _watched_targets()
    if not targets:
        return FAIL, "список наблюдаемых моделей пуст"
    bad = [m for m, p in targets.items()
           if any(('"' in x or "{" in x or len(x) > 60) for x in p)]
    detail = "; ".join(f"{m}:{sorted(p) or '—'}" for m, p in sorted(targets.items()))
    if bad:
        return FAIL, f"мусор в закреплённых провайдерах: {bad}"
    return OK, detail[:200]


async def chk_price_endpoints():
    if not FLAGS.net:
        return OK, "пропущено (нужен --net)"
    from app.config import settings
    from app.services.price_watch import _fetch_endpoints, _watched_targets
    from app.services.settings import Keys, get_setting
    targets = await _watched_targets()
    async with session_scope() as session:
        key = str(await get_setting(session, Keys.OPENROUTER_MANAGEMENT_KEY) or "")
    key = key or settings.openrouter_api_key
    got, zero = [], []
    for m in sorted(targets):
        eps = await _fetch_endpoints(m, key)
        (got if eps else zero).append(f"{m}:{len(eps)}")
    if zero and not got:
        return FAIL, "endpoints недоступны: " + ", ".join(zero)
    if zero:
        return WARN, "нет эндпоинтов у " + ", ".join(zero) + f" (есть: {', '.join(got)})"
    return OK, ", ".join(got)


async def chk_price_history():
    total = await q1("select count(*) from model_prices")
    agg = await q1("select count(distinct model) from model_prices where provider=''")
    prov = await q1("select count(*) from model_prices where provider<>''")
    if not total:
        return WARN, "история пуста — сверка ещё не выполнялась"
    return OK, f"строк {total} (агрегат по {agg} моделям, по провайдерам {prov})"


async def chk_price_settings():
    rows = await qall("select key, value from app_settings "
                      "where key like 'price_watch%' or key like 'price_alert%' order by key")
    if not rows:
        return WARN, "настройки price_watch не засеяны"
    return OK, ", ".join(f"{k.split('.')[-1]}={v}" for k, v in rows)[:200]


async def chk_price_run():
    if not FLAGS.write:
        return OK, "пропущено (нужен --write)"
    from app.services.price_watch import watch_models
    created = await watch_models()
    return OK, f"новых предупреждений: {created}"


# ------------------------------------------------- C. Очередь и статистика

async def chk_queue_rows():
    from app.web.routers import queue_page
    res = await queue_page._queue_rows()
    if not isinstance(res, tuple) or len(res) != 5:
        return FAIL, f"_queue_rows вернула {type(res).__name__}, элементов {len(res)}"
    rows, _sig, total, page, pages = res
    return OK, f"задач {total}, строк {len(rows)}, страница {page}/{pages}"


async def chk_jobs_states():
    rows = await qall("select state, count(*) from publish_jobs group by state order by 2 desc")
    return OK, ", ".join(f"{s}={n}" for s, n in rows) or "задач нет"


async def chk_in_progress():
    rows = await qall("select id, post_id from publish_jobs where state='in_progress' "
                      "order by id desc limit 5")
    if not rows:
        return OK, "нет задач в in_progress"
    return WARN, f"in_progress: {[(r[0], r[1]) for r in rows]} — см. recover_in_progress_jobs"


# ------------------------------------------------- D. Фильтры контента

SELFPROMO_PATS = ["нам на канал", "залил нам", "мы залили", "у нас на канале",
                  "нашем канале", "смотрите у нас", "мы добавили", "подписывайтесь"]
EVENT_MARKERS = ["регистрация по ссылке", "регистрация", "прямой эфир", "вебинар",
                 "начало в", "ждём вас", "места ограничены"]
EVENT_DOMAINS = ["timepad.ru", "webinar.ru", "zoom.us", "meetup.com"]

FOOTER_POST = (
    "🏡 Москва голосует рублём\n\n"
    "Похоже, для одних ипотека за пределами Москвы — квартира у моря.\n\n"
    "Источник: РБК Недвижимость\n\n"
    "⚡️ Новострой-М в [Дзене](https://dzen.ru/novostroy-m.ru) | "
    "[Телеграме](https://t.me/+kkXU6RTI0RNkYTYy) | Подписывайтесь"
)
EVENT_POST = (
    "🎙Прямой-эфир: Оценка площадки\n\n23 сентября,\nНачало в 19:30\n\n"
    "Регистрация:[ по ссылке](https://akademiya-developme-event.timepad.ru/event/4212782/)"
)


async def chk_selfpromo():
    from app.services.prefilter import _selfpromo_hit
    tail = _selfpromo_hit(FOOTER_POST, SELFPROMO_PATS)
    content = _selfpromo_hit("Залил нам на канал первую серию — смотрите у нас", SELFPROMO_PATS)
    if tail is not None:
        return FAIL, f"хвостовая подпись ошибочно принята за самопиар: {tail}"
    if not content:
        return FAIL, "контентный самопиар не распознан"
    return OK, f"подпись в хвосте пропущена; контентный самопиар -> {content}"


async def chk_event_promo():
    from app.services.prefilter import _event_promo_hit
    ev = _event_promo_hit(EVENT_POST, EVENT_MARKERS, EVENT_DOMAINS)
    news = _event_promo_hit(
        "Аналитики назвали пять частых ошибок инвесторов при оценке площадки под девелопмент.",
        EVENT_MARKERS, EVENT_DOMAINS)
    if not ev:
        return FAIL, "анонс мероприятия не распознан"
    if news:
        return FAIL, f"обычная новость ошибочно принята за анонс: {news}"
    return OK, f"анонс -> {ev}; новость пропущена"


async def chk_prompt_versions():
    rows = await qall("select prompt_version, count(*) from llm_calls "
                      "where stage='classify' and created_at > now() - interval '3 days' "
                      "group by 1 order by 2 desc")
    if not rows:
        return WARN, "нет вызовов classify за 3 дня"
    return OK, ", ".join(f"{v or '—'}={n}" for v, n in rows)[:200]


# ------------------------------------------------- E. Очистка подписей

CLEAN_LINES = ["Париж (VII округ). Улица Сен-Доминик, около 1900 года.", "",
               "👉[ Bspchannel. Подписаться](https://t.me/BSPhistorychannel)"]
CLEAN_SIG = "👉[ Bspchannel. Подписаться](https://t.me/BSPhistorychannel)"
FOOTER_LINES = ["Новость", "", "Источник: РБК Недвижимость", "",
                "⚡️ Канал в [Дзене](https://dzen.ru/x) | "
                "[Телеграме](https://t.me/+kkXU6RTI0RNkYTYy) | Подписывайтесь"]


async def chk_signature_lines():
    from app.services.llm_pipeline import _signature_lines
    got = _signature_lines(CLEAN_LINES, "BSPhistorychannel")
    if got != [3]:
        return FAIL, f"подпись источника: ожидали [3], получили {got}"
    got2 = _signature_lines(FOOTER_LINES, "startnedvizh")
    if got2 != [3, 5]:
        return FAIL, f"футер с площадками: ожидали [3, 5], получили {got2}"
    return OK, "подпись источника и футер с площадками распознаются"


async def chk_clean_plan():
    from app.services.llm_pipeline import _apply_clean_plan
    cases = [
        ([{"i": 3, "text": CLEAN_SIG}], "ok", [3], "точный план"),
        ([{"i": 1, "text": CLEAN_SIG}], "ok", [3], "неверный номер"),
        ([{"i": 3, "text": "Подписывайтесь на наш канал"}], "mismatch", [], "чужой текст"),
        ([{"i": 3, "text": ""}], "mismatch", [], "план без текста"),
        ([], "nothing", [], "пустой план"),
    ]
    bad = []
    for plan, want_status, want_drop, name in cases:
        _kept, dropped, status = _apply_clean_plan(CLEAN_LINES, plan)
        if status != want_status or dropped != want_drop:
            bad.append(f"{name}: {status}/{dropped} (ожидали {want_status}/{want_drop})")
    return (FAIL, "; ".join(bad)) if bad else (OK, f"{len(cases)} кейсов плана очистки")


async def chk_clean_stats():
    rows = await qall("select coalesce(details->>'mode','?'), count(*) from post_events "
                      "where action='clean_signatures' group by 1 order by 2 desc")
    fallback = await q1("select count(*) from post_events where action='clean_fallback_used'")
    failed = await q1("select count(*) from post_events where action='clean_verify_failed' "
                      "and created_at > now() - interval '1 day'")
    detail = (", ".join(f"{m}={n}" for m, n in rows) or "нет событий") + \
             f"; страховок {fallback}, провалов за сутки {failed}"
    return (WARN if failed else OK), detail


# ------------------------------------------------- F. Надёжность LLM

async def chk_no_verdict():
    n = await q1("select count(*) from posts where status='UNSUITABLE' "
                 "and coalesce(verdict_reason,'') like '%не дал ответа%'")
    return (FAIL if n else OK), f"{n} постов отклонено из-за отсутствия вердикта модели"


async def chk_parse_errors():
    total = await q1("select count(*) from llm_calls where created_at > now() - interval '1 day'")
    if not total:
        return WARN, "нет вызовов LLM за сутки"
    bad = await q1("select count(*) from llm_calls where created_at > now() - interval '1 day' "
                   "and status='parse_error'") or 0
    moderation = await q1("select count(*) from llm_calls where created_at > now() - interval '1 day' "
                          "and response->>'content' ilike '%User Safety%'") or 0
    share = 100.0 * bad / total
    detail = f"parse_error {bad}/{total} ({share:.1f}%), ответов модели модерации {moderation}"
    return (WARN if share > 20 else OK), detail


async def chk_fallback_settings():
    fb = await q1("select value from app_settings where key='llm.fallback_models'")
    cf = await q1("select value from app_settings where key='llm.clean_fallback_model'")
    if not fb:
        return WARN, "llm.fallback_models пуст — цепочка запасных моделей не задана"
    return OK, f"fallback_models={fb}; clean_fallback_model={cf or '—'}"


# ------------------------------------------------- G. Дедупликация

async def chk_dedup_rows():
    n = await q1("select count(*) from llm_calls where stage='dedup_confirm'")
    err = await q1("select count(*) from llm_calls where stage='dedup_confirm' and status<>'ok'")
    if not n:
        return WARN, "сверка полных текстов ещё не вызывалась"
    return (WARN if err else OK), f"вызовов {n}, ошибок {err}"


DEDUP_A = ("Новый рекламный ролик к фильму «Диггер» 🎥\n\n🗓️ Премьера состоится 2 октября.\n\n"
           "🎬[Киноредакция](https://t.me/kino_redakciya)")
DEDUP_B = ("Ведро для попкорна к фильму «Диггер» 🍿\n\n🗓️ Премьера состоится 2 октября.\n\n"
           "🎬[Киноредакция](https://t.me/kino_redakciya)")


async def chk_dedup_semantics():
    if not FLAGS.llm:
        return OK, "пропущено (нужен --llm)"
    from app.services.dedup import _confirm_same, _text_for_confirm

    class P:
        def __init__(self, t):
            self.original_text = t
            self.normalized_text = None

    ta, tb = _text_for_confirm(P(DEDUP_A)), _text_for_confirm(P(DEDUP_B))
    if "t.me/" in ta or "t.me/" in tb:
        return FAIL, f"ссылка источника не вычищена из текста сверки: {ta!r}"
    same = await _confirm_same(ta, tb)
    if same:
        return FAIL, "same=True: «трейлер» и «ведро для попкорна» приняты за одну новость"
    return OK, "same=False: разные факты при одном поводе различаются"


# ------------------------------------------------- H. Агрегатор (repost)

async def chk_repost_unique():
    dup = await qall("select post_id, count(*) from post_events where action='repost_sent' "
                     "group by 1 having count(*)>1 limit 5")
    recent = await qall("select post_id, count(*) from post_events where action='repost_queued' "
                        "and created_at > now() - interval '1 day' group by 1 having count(*)>1 limit 5")
    if dup:
        return FAIL, f"несколько пересылок у постов {[r[0] for r in dup]}"
    if recent:
        return WARN, f"несколько постановок в очередь за сутки: {[r[0] for r in recent]}"
    return OK, "одна пересылка на пост"


async def chk_aggregate_calls():
    rows = await qall("select post_id, count(*) c from llm_calls "
                      "where prompt_version like 'aggregate%' group by 1 order by c desc limit 3")
    if rows and rows[0][1] > 3:
        return WARN, f"до {rows[0][1]} вызовов фильтра на пост #{rows[0][0]}"
    return OK, ", ".join(f"#{p}:{c}" for p, c in rows) or "вызовов фильтра не было"


async def chk_repost_pending():
    n = await q1("select count(*) from posts where repost_pending")
    if n and n > 5:
        return WARN, f"в очереди пересылки {n} постов"
    return OK, f"в очереди пересылки {n}"


async def chk_aggregate_config():
    rows = await qall("select username, aggregate_mode, aggregate_min_score, daily_limit, "
                      "min_interval_min, aggregate_accept is not null, aggregate_reject is not null "
                      "from target_channels where no_review")
    if not rows:
        return WARN, "нет технических каналов (no_review)"
    bad = [r[0] for r in rows if r[1] == "repost" and (r[3] is None or r[4] is None)]
    detail = "; ".join(f"{r[0]}:{r[1]},min={r[2]},lim={r[3]},int={r[4]},"
                       f"acc={'да' if r[5] else 'нет'},rej={'да' if r[6] else 'нет'}" for r in rows)
    return (FAIL if bad else OK), detail[:200]


# ------------------------------------------------- I. Действия владельца

async def chk_delete_interlock():
    from app.services.review import hard_delete
    from app.services.security import is_hard_delete_armed
    armed = await is_hard_delete_armed()
    res = await hard_delete(999999999)
    expected = "пост не найден" if armed else "предохранитель"
    if expected not in res.message:
        return FAIL, f"armed={armed}, неожиданный ответ: {res.message}"
    return OK, f"armed={armed}; отказ корректен ({res.message})"


async def chk_restart_guard():
    from app.services.review import restart_pipeline
    res = await restart_pipeline(999999999)
    if res.ok or "не найден" not in res.message:
        return FAIL, f"неожиданный ответ: {res.message}"
    return OK, "несуществующий пост не перезапускается"


# ------------------------------------------------- J. Публикация

async def chk_published_text():
    row = (await qall("select count(*) total, count(*) filter (where p.published_text is not null) w "
                      "from publish_jobs j join posts p on p.id=j.post_id "
                      "where j.state='done' and j.published_at > now() - interval '3 days'"))[0]
    total, with_text = int(row[0] or 0), int(row[1] or 0)
    if not total:
        return WARN, "не было публикаций за 3 дня"
    if not with_text:
        return WARN, f"{total} публикаций без published_text (фиксация текста не работает?)"
    return OK, f"{with_text}/{total} публикаций с сохранённым текстом"


async def chk_repost_marker():
    n = await q1("select count(*) from posts where published_links @> '[{\"kind\":\"repost\"}]'::jsonb "
                 "and published_text is not null")
    if n:
        return WARN, f"{n} repost-постов с продублированным текстом (ожидается null)"
    return OK, "repost-посты не дублируют текст оригинала"


async def chk_publish_settings():
    rows = await qall("select key, value from app_settings where key like 'publish.%' order by key")
    have = {k for k, _v in rows}
    missing = {"publish.max_media_mb"} - have
    detail = ", ".join(f"{k.split('.')[-1]}={v}" for k, v in rows)[:200]
    return (WARN if missing else OK), (f"нет {sorted(missing)}; " if missing else "") + detail


async def chk_media_sizes():
    row = await qall("select count(*) filter (where size_bytes > 50*1024*1024) big, "
                     "coalesce(max(size_bytes),0)/(1024*1024) max_mb from media_items")[0]
    if int(row[0] or 0):
        return WARN, f"{row[0]} файлов больше 50 МБ (максимум {int(row[1])} МБ) — проверьте лимиты"
    return OK, f"файлов больше 50 МБ нет (максимум {int(row[1])} МБ)"


# ------------------------------------------------- K. Reader

async def chk_unique_source_message():
    rows = await qall("select source_id, source_message_id, count(*) from posts "
                      "group by 1,2 having count(*)>1 limit 5")
    return (FAIL, f"дубли сообщений: {rows}") if rows else (OK, "(source, message) уникальны")


async def chk_text_guard():
    n = await q1("select count(*) from posts p1 join posts p2 "
                 "on p2.text_hash=p1.text_hash and p2.source_id=p1.source_id and p2.id>p1.id "
                 "where p1.created_at > now() - interval '1 day'")
    if n:
        return WARN, f"{n} пар с одинаковым текстом из одного источника за сутки"
    return OK, "повторы текста из источника не создаются"


async def chk_stuck_new():
    n = await q1("select count(*) from posts p left join sources s on s.id=p.source_id "
                 "where p.status='NEW' and p.created_at < now() - interval '2 hours' "
                 "and (s.editorial_only is null or s.editorial_only = false)")
    return (WARN if n else OK), f"{n} постов в NEW старше 2 часов"


async def chk_media_download():
    rows = await qall("select coalesce(left(download_error, 20), 'ok'), count(*) "
                      "from media_items group by 1 order by 2 desc limit 4")
    return OK, ", ".join(f"{e}={n}" for e, n in rows) or "медиа нет"


async def chk_sources_fresh():
    rows = await qall("select username, round(extract(epoch from (now() - last_read_at))/60) "
                      "from sources where enabled order by last_read_at nulls first limit 5")
    if not rows:
        return WARN, "нет включённых источников"
    stale = [r for r in rows if r[1] is None or int(r[1]) > 360]
    detail = ", ".join(f"{r[0]}:{'—' if r[1] is None else str(int(r[1])) + 'м'}" for r in rows)
    return (WARN if stale else OK), detail


CHECKS = (
    ("A1", "alembic: версия БД = heads", chk_alembic),
    ("A2", "импорты модулей", chk_imports),
    ("A3", "redis доступен", chk_redis),
    ("A4", "price_watch: отметка живости", chk_price_stamp),
    ("A5", "диск: свободное место", chk_disk),
    ("A6", "память: MemAvailable", chk_mem),
    ("B1", "price watch: наблюдаемые модели/провайдеры", chk_price_targets),
    ("B2", "price watch: endpoints провайдеров", chk_price_endpoints),
    ("B3", "price watch: история цен", chk_price_history),
    ("B4", "price watch: настройки", chk_price_settings),
    ("B5", "price watch: прогон сверки", chk_price_run),
    ("C1", "очередь: _queue_rows без ошибок", chk_queue_rows),
    ("C2", "очередь: состояния задач", chk_jobs_states),
    ("C3", "очередь: нет зависших in_progress", chk_in_progress),
    ("D1", "фильтр: самопиар (хвост vs контент)", chk_selfpromo),
    ("D2", "фильтр: анонсы мероприятий", chk_event_promo),
    ("D3", "версии промптов классификации", chk_prompt_versions),
    ("E1", "очистка: детектор строк-подписей", chk_signature_lines),
    ("E2", "очистка: сверка плана модели", chk_clean_plan),
    ("E3", "очистка: статистика режимов", chk_clean_stats),
    ("F1", "LLM: нет отказов без вердикта", chk_no_verdict),
    ("F2", "LLM: доля parse_error и модель модерации", chk_parse_errors),
    ("F3", "LLM: цепочка запасных моделей", chk_fallback_settings),
    ("G1", "дедуп: вызовы сверки полных текстов", chk_dedup_rows),
    ("G2", "дедуп: семантика (трейлер ≠ попкорн)", chk_dedup_semantics),
    ("H1", "агрегатор: одна пересылка на пост", chk_repost_unique),
    ("H2", "агрегатор: вызовов фильтра на пост", chk_aggregate_calls),
    ("H3", "агрегатор: очередь пересылок", chk_repost_pending),
    ("H4", "агрегатор: конфигурация канала", chk_aggregate_config),
    ("I1", "предохранитель полного удаления", chk_delete_interlock),
    ("I2", "перезапуск: защита от чужого поста", chk_restart_guard),
    ("J1", "публикация: текст сохранён", chk_published_text),
    ("J2", "публикация: repost без дубля текста", chk_repost_marker),
    ("J3", "публикация: настройки лимитов", chk_publish_settings),
    ("J4", "медиа: файлы больше 50 МБ", chk_media_sizes),
    ("K1", "reader: уникальность (source, message)", chk_unique_source_message),
    ("K2", "reader: guard повторов текста", chk_text_guard),
    ("K3", "reader: нет зависших NEW", chk_stuck_new),
    ("K4", "reader: ошибки загрузки медиа", chk_media_download),
    ("K5", "reader: свежесть опроса источников", chk_sources_fresh),
)


async def run_check(cid: str, name: str, fn) -> None:
    try:
        status, detail = await fn()
    except Exception as exc:  # noqa: BLE001
        status, detail = FAIL, f"{exc.__class__.__name__}: {exc}"
    RESULTS.append((cid, name, status, str(detail or "")[:220]))


def report() -> int:
    fails = [r for r in RESULTS if r[2] == FAIL]
    warns = [r for r in RESULTS if r[2] == WARN]
    if FLAGS.json:
        print(json.dumps([{"id": c, "name": n, "status": s, "detail": d}
                          for c, n, s, d in RESULTS], ensure_ascii=False, indent=1))
        return 1 if fails else 0
    marks = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
    width = max(len(n) for _c, n, _s, _d in RESULTS)
    for cid, name, status, detail in RESULTS:
        print(f"{marks[status]} {cid:<3} {name.ljust(width)}  {detail}")
    print("-" * (width + 40))
    print(f"итого: OK {len(RESULTS) - len(fails) - len(warns)}, WARN {len(warns)}, FAIL {len(fails)}"
          f"  (флаги: net={int(FLAGS.net)} llm={int(FLAGS.llm)} write={int(FLAGS.write)})")
    if fails:
        print("провалены:", ", ".join(c for c, _n, s, _d in RESULTS if s == FAIL))
    return 1 if fails else 0


async def main() -> int:
    for _cid, name, fn in CHECKS:
        await run_check(_cid, name, fn)
    return report()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Проверки после деплоя")
    parser.add_argument("--net", action="store_true", help="внешние HTTP-запросы")
    parser.add_argument("--llm", action="store_true", help="один дешёвый вызов модели")
    parser.add_argument("--write", action="store_true", help="разрешить запись (watch_models)")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    FLAGS = parser.parse_args(namespace=FLAGS)
    sys.exit(asyncio.run(main()))