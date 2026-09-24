# Чек-лист проверок TG Content Hub

## Подготовка (один раз на сессию)

```bash
cd ~/tg-content-hub
PSQL="docker compose exec -T postgres psql -U content_hub -d content_hub"
Q="$PSQL -tAc"
```

Правила написания проверок:
- в одном процессе `docker compose run … python` — ровно один `asyncio.run()` (иначе пул соединений БД остаётся привязан к закрытому event loop);
- heredoc внутри `sh -c` — только с закавыченным разделителем (`<<'PY'`), иначе `${…}` из f-строк ломает shell (`Bad substitution`);
- длинные проверки лучше держать в `scripts/` и запускать как `docker compose run --rm --entrypoint python api scripts/<file>.py` (после `docker compose build`).

## A. Smoke после каждого деплоя

```bash
docker compose ps --format 'table {{.Name}}\t{{.Status}}'      # все Up, без Restaring; migrate Exited(0)
$Q "select version_num from alembic_version"                    # 0033
docker compose run --rm --entrypoint python api -c "
import app.web.main, app.services.llm_pipeline, app.services.publishing, app.services.review, \
       app.services.price_watch, app.services.dedup, app.services.prefilter, app.services.security, \
       app.services.queue, app.workers.reader, app.workers.scheduler, app.services.editorial_journalist
print('imports ok')"
docker compose logs api pipeline reader scheduler bot --tail=120 \
  | grep -Ei "traceback|NameError|ImportError|Timeout connecting" | head   # пусто
docker compose exec redis redis-cli get price_watch:last_run    # отметка не старше 24 ч
docker compose exec redis redis-cli ping                        # PONG
df -h / | tail -1                                              # Avail >= 1.5G
free -m | head -2                                              # available >= 300 МБ
```

## B. Контроль цен моделей (price watch, разрез по провайдерам)

```bash
# B.1 точки наблюдения: модели, закреплённые провайдеры, цены эндпоинтов
docker compose run --rm --entrypoint python api scripts/check_provider_prices.py
#   ожидание: режим pinned; «закреплено» — имена провайдеров из order/only (не обрывки JSON);
#   эндпоинтов > 0 у каждой модели (404 значит неверный slug); «выбрано» — все закреплённые

# B.2 прогон сверки
docker compose run --rm --entrypoint python api -c "
import asyncio
from app.services.price_watch import watch_models
async def main():
    print('новых предупреждений:', await watch_models())
asyncio.run(main())"

# B.3 история цен (агрегат provider='' и по провайдерам)
$Q "select model, provider, round((prompt_usd*1000000)::numeric,3) as in_1m, round((completion_usd*1000000)::numeric,3) as out_1m, web_search_usd, fetched_at from model_prices order by id desc limit 15"
$Q "select id, model, provider, direction, change_pct, acknowledged from model_price_alerts order by id desc limit 5"
$Q "select key, value from app_settings where key like 'price_watch%' or key like 'price_alert%'"

# B.4 живость фонового цикла и режим
docker compose exec redis redis-cli get price_watch:last_run
grep -n "price_watch" app/web/main.py            # import + create_task(price_watch.loop())

# B.5 имитация роста цены у конкретного провайдера -> баннер
$Q "update model_prices set prompt_usd=prompt_usd/2 where provider<>'' and id in (select max(id) from model_prices where provider<>'' group by model, provider)"
docker compose run --rm --entrypoint python api -c "
import asyncio
from app.services.price_watch import watch_models
async def main():
    print('создано:', await watch_models())
asyncio.run(main())"
#   в браузере: оранжевый баннер «Цена <модель [Провайдер]> выросла на N%», «Понятно» гасит его
$Q "select model, provider, change_pct, acknowledged from model_price_alerts order by id desc limit 3"

# B.6 уборка после теста
$Q "delete from model_price_alerts; delete from model_prices;"
docker compose run --rm --entrypoint python api -c "
import asyncio
from app.services.price_watch import watch_models
async def main():
    await watch_models()
asyncio.run(main())"
$Q "select count(*) from model_prices"           # по строке на точку наблюдения
```

Замечания: `HTTP 403` у endpoints = нужен `OPENROUTER_MANAGEMENT_KEY` в `.env`; дрейф < `price_watch.history_min_change_pct` в историю не пишется; порог предупреждения — `price_alert_pct`.

## C. Очередь и статистика

```bash
docker compose logs api --tail=200 | grep -E "500 Internal|NameError|too many values" | head   # пусто
$Q "select state, count(*) from publish_jobs group by 1"
$Q "select count(*) from publish_jobs where state='in_progress'"   # 0, если ничего не публикуется
```

В браузере: `/queue` открывается, пагинация и автообновление работают; `/stats` показывает блок «Очистка подписей (clean)» с долей страховок.

## D. Фильтры контента: самопиар, анонсы мероприятий, подписи

```bash
docker compose run --rm --entrypoint python api -c "
from app.services.prefilter import _selfpromo_hit, _event_promo_hit
pats=['нам на канал','залил нам','мы залили','у нас на канале','нашем канале','смотрите у нас','мы добавили','подписывайтесь']
mk=['регистрация по ссылке','регистрация','прямой эфир','вебинар','начало в','ждём вас','места ограничены']
dom=['timepad.ru','webinar.ru','zoom.us','meetup.com']
sig='🏡 Москва голосует рублём\n\nПохоже, для одних ипотека за пределами Москвы — квартира у моря.\n\nИсточник: РБК Недвижимость\n\n⚡️ Новострой-М в [Дзене](https://dzen.ru/novostroy-m.ru) | [Телеграме](https://t.me/+kkXU6RTI0RNkYTYy) | Подписывайтесь'
ev='🎙Прямой-эфир: Оценка площадки\n\n23 сентября,\nНачало в 19:30\n\nРегистрация:[ по ссылке](https://akademiya-developme-event.timepad.ru/event/4212782/)'
news='Аналитики назвали пять частых ошибок инвесторов при оценке площадки под девелопмент.'
print('подпись в хвосте    ->', _selfpromo_hit(sig, pats))     # None
print('контентный самопиар ->', _selfpromo_hit('Залил нам на канал первую серию — смотрите у нас', pats))
print('анонс мероприятия   ->', _event_promo_hit(ev, mk, dom))  # регистрация + timepad
print('обычная новость     ->', _event_promo_hit(news, mk, dom)) # None
"
$Q "select count(*) from post_events where action='selfpromo_blocked' and created_at > now() - interval '1 day'"
$Q "select count(*) from post_events where action='event_promo_blocked' and created_at > now() - interval '1 day'"
$Q "select prompt_version, count(*) from llm_calls where stage='classify' and created_at > now() - interval '1 day' group by 1"
#   ожидание: classify-v10 и aggregate-v5; ложных selfpromo_blocked — единицы, не десятки
```

## E. Очистка подписей (clean-v3: детерминированно + страховка)

```bash
docker compose run --rm --entrypoint python api -c "
from app.services.llm_pipeline import _signature_lines, _apply_clean_plan, _finalize_clean
lines=['Париж (VII округ). Улица Сен-Доминик, около 1900 года.','','👉[ Bspchannel. Подписаться](https://t.me/BSPhistorychannel)']
sig=_signature_lines(lines,'BSPhistorychannel'); print('подписи:', sig)                    # [3]
kept=[l for i,l in enumerate(lines,1) if i not in set(sig)]
print('черновик:', repr(_finalize_clean(kept, '\n'.join(lines))))
fot=['Новость','','Источник: РБК Недвижимость','','⚡️ Канал в [Дзене](https://dzen.ru/x) | [Телеграме](https://t.me/+kkXU6RTI0RNkYTYy) | Подписывайтесь']
print('футер с площадками:', _signature_lines(fot,'startnedvizh'))              # [3, 5]
print('план точный     :', _apply_clean_plan(lines,[{'i':3,'text':'👉[ Bspchannel. Подписаться](https://t.me/BSPhistorychannel)'}]))
print('план неверный № :', _apply_clean_plan(lines,[{'i':1,'text':'👉[ Bspchannel. Подписаться](https://t.me/BSPhistorychannel)'}]))
print('план чужой текст:', _apply_clean_plan(lines,[{'i':3,'text':'Подписывайтесь на наш канал'}]))   # mismatch
print('план без текста :', _apply_clean_plan(lines,[{'i':3,'text':''}]))                        # mismatch
print('пустой план     :', _apply_clean_plan(lines,[]))                                         # nothing
"
$Q "select details->>'mode' as mode, count(*) from post_events where action='clean_signatures' group by 1"   # преобладает deterministic
$Q "select count(*) from post_events where action='clean_fallback_used'"
$Q "select count(*) from post_events where action='clean_verify_failed'"
$Q "select model, status, count(*) from llm_calls where prompt_version like 'clean%' group by 1,2"
```

В браузере: у поста со страховкой — оранжевая плашка в шапке; у провала сверки — красноватая `⛔` и статус `NEEDS_MANUAL_REVIEW`.

## F. Надёжность LLM (модель модерации, цепочка запасных, «нет вердикта ≠ отклонить»)

```bash
$Q "select stage, status, count(*) from llm_calls where created_at > now() - interval '1 day' group by 1,2 order by 1"
$Q "select model, count(*) from llm_calls where status='parse_error' and created_at > now() - interval '1 day' group by 1 order by 2 desc"
docker compose logs pipeline editorial --tail=300 | grep -Ei "ответ модели модерации|пробуем следующую|без вердикта" | tail
$Q "select count(*) from posts where status='UNSUITABLE' and coalesce(verdict_reason,'') like '%не дал ответа%'"   # 0
$Q "select count(*) from post_events where action in ('aggregate_no_verdict','aggregate_giveup') and created_at > now() - interval '1 day'"
$Q "select key, value from app_settings where key in ('llm.fallback_models','llm.clean_fallback_model')"
```

## G. Дедупликация: сверка по полным текстам (dedup-confirm-v2)

```bash
docker compose run --rm --entrypoint python api -c "
import asyncio
from app.services.dedup import _confirm_same, _text_for_confirm
class P:
    def __init__(s,t): s.original_text=t; s.normalized_text=None
a='Новый рекламный ролик к фильму «Диггер» 🎥\n\n🗓️ Премьера состоится 2 октября.\n\n🎬[Киноредакция](https://t.me/kino_redakciya)'
b='Ведро для попкорна к фильму «Диггер» 🍿\n\n🗓️ Премьера состоится 2 октября.\n\n🎬[Киноредакция](https://t.me/kino_redakciya)'
async def main():
    print('A:', repr(_text_for_confirm(P(a))))
    print('same (ожидается False):', await _confirm_same(_text_for_confirm(P(a)), _text_for_confirm(P(b))))
asyncio.run(main())"
$Q "select status, count(*), round(sum(coalesce(cost_usd,0))::numeric,5) from llm_calls where stage='dedup_confirm' group by 1"
$Q "select key, value from app_settings where key like 'dedup.confirm%'"
$Q "select count(*) from post_events where action in ('deduplicated','dedup_cleared') and created_at > now() - interval '1 day'"
```

В карточке: «Дубликат поста #N» (номер кликабельный), «🔎 Подтверждение дубля» с `text_a`/`text_b` в details.

## H. Агрегатор (repost): одна пересылка на пост + плавность

```bash
$Q "select post_id, count(*) from post_events where action='repost_sent' group by 1 having count(*)>1"     # пусто
$Q "select post_id, count(*) from post_events where action='repost_queued' group by 1 having count(*)>1"   # пусто
$Q "select post_id, count(*) from llm_calls where prompt_version like 'aggregate%' group by 1 order by 2 desc limit 5"   # 1-2
$Q "select count(*) from posts where repost_pending"                                                      # 0 в покое
$Q "select state, count(*) from publish_jobs where target_channel_id=(select id from target_channels where username='moskva_novostroy') group by 1"
$Q "select username, aggregate_mode, aggregate_min_score, daily_limit, min_interval_min from target_channels where no_review"
docker compose logs reader --tail=200 | grep -E "переслан|пересылка отложена|пересылка отменена|сбой очереди" | tail -10
```

## I. Действия владельца: перезапуск, удаление, предохранитель

```bash
make rm_post_status                               # armed = False, ttl = -2
docker compose run --rm --entrypoint python api -c "
import asyncio
from app.services.review import hard_delete
async def main():
    print(await hard_delete(999999))
asyncio.run(main())"                               # «полное удаление отключено (предохранитель)»
make rm_post_true
docker compose run --rm --entrypoint python api -c "
import asyncio
from app.services.review import hard_delete
async def main():
    print(await hard_delete(999999))
asyncio.run(main())"                               # «пост не найден» — предохранитель снят
$Q "select id, status from posts order by id desc limit 5"   # выбрать тестовый ID
docker compose run --rm --entrypoint python api -c "
import asyncio
from app.services.review import hard_delete
async def main():
    print(await hard_delete(<ID>))
asyncio.run(main())"
for t in posts media_items post_events post_draft_versions llm_calls publish_jobs; do
  echo -n "$t: "; $Q "select count(*) from $t where post_id=<ID>"; done    # везде 0
make rm_post_false
```

В браузере: `/posts/<ID>` → «Пост не найден» (404); кнопка «🗑 Удалить навсегда» серая при выключенном предохранителе; «🔁 Повторить обработку» сбрасывает в `NEW` (событие `restart_pipeline` со списком очищенного), для `PUBLISHED/APPROVED/SCHEDULED/PUBLISHING` — заблокирована.

## J. Публикация: текст, медиа, сжатие, восстановление задач

```bash
# J.1 опубликованный текст
$Q "select id, status, length(published_text) as chars, jsonb_array_length(coalesce(published_links,'[]'::jsonb)) as links from posts where published_text is not null order by id desc limit 5"
$Q "select id, published_text is null as text_null, published_links->0->>'kind' as kind from posts where published_links @> '[{\"kind\":\"repost\"}]'::jsonb order by id desc limit 5"   # t|repost

# J.2 лимиты медиа и режимы
$Q "select key, value from app_settings where key like 'publish.%' or key like 'reader.max_media%'"
$Q "select id, round(size_bytes/1048576.0,1) as mb, downloaded from media_items order by size_bytes desc nulls last limit 5"
$Q "select id, state, attempts, left(coalesce(last_error,''),80) as err from publish_jobs order by id desc limit 5"
docker compose logs scheduler --tail=200 | grep -Ei "начинаю публикацию|медиа больше лимита|EntityTooLarge|ffmpeg|сжато|пропущены" | tail -10
$Q "select count(*) from post_events where action like 'media_compress%'"

# J.3 сжатие без публикации (прямой вызов, безопасно)
docker compose exec api sh -c "ls -la /app/media | head"     # найти тестовый файл
docker compose run --rm --entrypoint python api -c "
import asyncio, time
from app.services.publishing import _compress_video, _file_size, _media_root, _safe_unlink
src = _media_root() / '<source_id>/<message_id>/<file>.mp4'
async def main():
    print('исходник МБ:', round(_file_size(src)/1048576, 1))
    t = time.time(); out = await _compress_video(src, 30, 854)
    print('сек:', round(time.time()-t,1), '| результат МБ:', round(_file_size(out)/1048576,1) if out else None)
    if out: _safe_unlink(out)
asyncio.run(main())"

# J.4 восстановление «сиротских» in_progress после перезапуска планировщика
$Q "update publish_jobs set state='in_progress' where id=<ID_ЗАДАЧИ>"
docker compose restart scheduler
docker compose logs scheduler --tail=40 | grep -E "восстановлены задачи|начинаю публикацию"
$Q "select id, state, left(coalesce(last_error,''),60) from publish_jobs where id=<ID_ЗАДАЧИ>"   # queued
docker compose exec api sh -c "du -sh /app/media; df -h /app/media | tail -1"
```

В карточке: во время сжатия у задачи `in_progress` и «Причина: сжатие видео ffmpeg: …», страница обновляется сама; у repost-постов блок «Опубликованный текст» = «Репост оригинала» + ссылка на пересылку; шапка показывает «⏳ Публикация не завершена…» пока задача в работе и «🤖 Опубликовано автопилотом» только после `done`.

## K. Reader: очередь, повторы источника, лимит загрузки медиа

```bash
$Q "select source_id, source_message_id, count(*) from posts group by 1,2 having count(*)>1"   # пусто (уникальность не нарушена)
$Q "select count(*) from posts p1 join posts p2 on p2.text_hash=p1.text_hash and p2.source_id=p1.source_id and p2.id>p1.id where p1.created_at > now() - interval '1 day'"   # 0: повторы текста не создаются
docker compose logs reader --tail=200 | grep -E "текст уже есть в посте|enqueue_post|size_limit|сбой очереди" | tail -10
$Q "select count(*) from posts where status='NEW' and created_at < now() - interval '2 hours'"   # ~0: рескан подбирает
$Q "select local_path is null as no_file, left(coalesce(download_error,''),40) as err, count(*) from media_items group by 1,2 order by 3 desc limit 5"
```

## L. Веб-интерфейс (руками, 3–4 минуты)

- `/posts`: чекбоксы каналов, счётчик «Все каналы (+N / -M)», применение только по «Фильтр», память между заходами, `hide` в пагинации.
- Карточка поста: автоперезагрузка при смене статуса (блок «Действия» всегда соответствует статусу); «Вызовы LLM» — стадия/статус/модель/стоимость/сырой ответ; ссылки в «Дедупликации» и «Ранее писали» кликабельны; «Опубликованный текст» + «Ссылки публикации».
- `/queue`:状态 задачи, примечания (в т.ч. «сжатие видео ffmpeg: …»), отмена queued/scheduled.
- `/settings`: группы («Цены моделей», «Публикация», «Дедупликация», «Префильтр и медиа»), «Сохранить всё» сохраняет изменения во всех группах.
- `/stats`: графики + блок «Очистка подписей (clean)».
- Баннер цен: появляется при активном алерте, «Понятно» гасит и ставит `acknowledged=t`.

## M. Инфраструктура и нагрузка

```bash
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'
free -m | head -2
sudo dmesg -T | grep -iE "oom|killed process" | tail -5         # пусто
$Q "show max_connections" ; $Q "select count(*) from pg_stat_activity"   # запас соединений
docker compose logs --since 6h | grep -ciE "Timeout connecting|ConnectionRefused"   # 0–1
uptime
```

Правило: не держать `publish_media_compress=1` на слабом сервере и не запускать тяжёлые проверки во время публикации больших видео.

## N. Резервные копии

```bash
mkdir -p ~/backups && docker compose exec -T postgres pg_dump -U content_hub -d content_hub | gzip > ~/backups/ch_$(date +%F).sql.gz
ls -lh ~/backups | tail -3                         # размер вменяемый (не 0 байт)
# проверка восстановления (на временном контейнере, НЕ на рабочем):
#   docker run --rm -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
#   gunzip -c ~/backups/ch_YYYY-MM-DD.sql.gz | psql "postgresql://postgres:test@127.0.0.1:55432/postgres"
du -sh data/media
```

## O. Ещё не автоматизировано (проверять вручную при изменениях)

1. Политика свежести reader: `fresh_window_min`, `fallback_count`, `fallback_max_age_hours`; курсор `last_read_message_id` движется.
2. Медиа-дедупликация: pHash-расстояние, допуск яркости, `MEDIA_TEXT_FLOOR` против коллизий dHash.
3. Блок «Ранее об этом писали»: `recap_ids` только для опубликованных дублей в окне `publish_dup_recap_window_hours`; `dup_recap` per-channel.
4. Автопилот и двойная проверка: `autopilot_min_score`, `double_check_online` vs offline, `doublecheck-v5`; предохранитель подписей `autopilot_sig_guard`.
5. Сохранение ссылок сквозь конвейер: markdown → нативные text_link; страховка «Подробнее: url» при `publish_restore_links=1`.
6. Пер-источниковые `filters`/`llm_instructions` и пер-канальные `llm_instructions` (видны в `request.messages[0].content`).
7. Бюджеты: `max_llm_budget_usd_per_day`, `max_candidates_per_day`, `editorial_budget_usd_per_day`; redis-ключи `guard:*`.
8. `make yaml-check` и идемпотентный `make sources-sync` (поля no_review/aggregate_*/llm_instructions доезжают до БД).
9. Виртуальная редакция: циклы `editorial_cycle_times`, `browse_enabled=0` → нет вызовов `:online`, страница `/editorial`.
10. Уведомления: `card_sent` один раз на версию черновика, webpush, очистка клавиатуры после публикации.
11. Мониторинг: `/api/status` и верхняя плашка проблем при остановке воркера.