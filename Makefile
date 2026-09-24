.PHONY: up down restart logs ps migrate revision psql health test login source-add source-list source-disable rm_post_true rm_post_false rm_post_status verify verify-full verify-json

up:            ## собрать и запустить всё
	docker compose up -d --build

down:          ## остановить
	docker compose down

restart:
	docker compose restart

logs:          ## хвосты логов всех сервисов
	docker compose logs -f --tail=100

logs2:         ## краткие логи: только сервисы приложения (без postgres/redis)
	docker compose logs -f --tail=100 reader pipeline bot scheduler api

logs3:         ## все логи минус инфраструктурный шум (redis save, pg checkpoints, httpx, access 200)
	docker compose logs -f --tail=200 | grep -vE --line-buffered 'Background saving|Saving\.\.\.|DB saved on disk|Fork CoW|changes in [0-9]+ seconds|checkpoint (starting|complete)|httpx: HTTP Request|telethon\.|HTTP/1\.1" 200|GET /media/|GET /api/|GET /healthz|GET /favicon'

ps:
	docker compose ps

migrate:       ## применить миграции вручную
	docker compose run --rm migrate

revision:      ## новая автогенерируемая миграция: make revision m="описание"
	docker compose run --rm migrate alembic revision --autogenerate -m "$(m)"

psql:
	docker compose exec postgres psql -U content_hub -d content_hub

health:        ## проверка этапа 0
	curl -s http://127.0.0.1:8000/healthz

test:          ## локальные тесты без docker (нужен venv с зависимостями)
	pytest -q

login:         ## одноразовый интерактивный вход аккаунта-читателя
	docker compose run --rm reader python -m app.auth.login

source-add:    ## пример: make source-add USERNAME=@my_test_lab KIND=test
	docker compose run --rm migrate python -m app.cli.sources add $(USERNAME) --kind $(KIND)

source-list:   ## список источников
	docker compose run --rm migrate python -m app.cli.sources list

source-disable: ## пример: make source-disable USERNAME=@some_channel
	docker compose run --rm migrate python -m app.cli.sources set-enabled $(USERNAME) false

source-delete: ## пример: make source-delete USERNAME=@канал [CASCADE]
	docker compose run --rm migrate python -m app.cli.sources delete $(USERNAME) $(MODE)

sources-sync:  ## применить sources.yaml к базе
	docker compose run --rm migrate python -m app.cli.sources sync

yaml-check:  ## проверить sources.yaml без применения (синтаксис + ссылки)
	docker compose run --rm migrate python -c "import app.services.sources_sync as ss; print(ss.report(*ss.load_sources_file('sources.yaml')))"

llm-check:     ## проверить OpenRouter и слаги моделей (до обработки постов)
	docker compose run --rm migrate python -m app.cli.llm_check

llm-models:    ## эндпоинты модели: провайдеры и цены: make llm-models MODEL=openai/gpt-5.6-luna
	docker compose run --rm migrate python -m app.cli.llm_models $(MODEL)

target-list:   ## список целевых каналов
	docker compose run --rm migrate python -m app.cli.sources target-list

wait-web:    ## ждать готовности веб-админки после make up (до ~30 мин)
	@for i in $$(seq 1 20); do \
	  code=$$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/healthz 2>/dev/null); \
	  if [ "$$code" = "200" ]; then printf "\033[32m✓ api готов — можно открывать http://127.0.0.1:8000/\033[0m\n"; exit 0; fi; \
	  printf "⏳ api ещё не готов… (попытка %d/20, следующая проверка через 90 с)\n" $$i; \
	  sleep 90; \
	done; \
	printf "\033[31m✗ api не ответил за ~30 мин — смотрите docker compose logs api\033[0m\n"; exit 1

fix-media: ## опубликованные посты: превью вместо тяжёлых оригиналов
	docker compose run --rm --entrypoint "python -m app.tools.backfill_previews" api

clean-media: ## чистка тома медиа: удалить ненужные оригиналы (превью и нужные для публикации не трогает)
	docker compose run --rm --entrypoint "python -m app.tools.clean_media_volume" api


.PHONY: rm_post_true rm_post_false rm_post_status

rm_post_true: ## включить возможность полного удаления постов в админке (30 мин)
	docker compose exec -T api python -c "import asyncio;from app.services.security import set_hard_delete_armed;asyncio.run(set_hard_delete_armed(True));print('предохранитель ВКЛЮЧЁН на 30 минут')"

rm_post_false: ## выключить возможность полного удаления постов в админке
	docker compose exec -T api python -c "import asyncio;from app.services.security import set_hard_delete_armed;asyncio.run(set_hard_delete_armed(False));print('предохранитель выключен')"

rm_post_status: ## показать состояние предохранителя
	docker compose exec -T api python -c "import asyncio;from app.services.security import is_hard_delete_armed;print('armed =', asyncio.run(is_hard_delete_armed()))"
	docker compose exec -T redis redis-cli ttl admin:hard_delete_armed

verify: ## автоматические проверки после деплоя (read-only)
	docker compose run --rm --entrypoint python api scripts/verify.py

verify-full: ## проверки + внешние запросы OpenRouter + один вызов модели
	docker compose run --rm --entrypoint python api scripts/verify.py --net --llm --write

verify-json: ## то же, что verify, но вывод в JSON
	docker compose run --rm --entrypoint python api scripts/verify.py --json