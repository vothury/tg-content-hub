.PHONY: up down restart logs ps migrate revision psql health test login source-add source-list source-disable

up:            ## собрать и запустить всё
	docker compose up -d --build

down:          ## остановить
	docker compose down

restart:
	docker compose restart

logs:          ## хвосты логов всех сервисов
	docker compose logs -f --tail=100

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

llm-check:     ## проверить OpenRouter и слаги моделей (до обработки постов)
	docker compose run --rm migrate python -m app.cli.llm_check

llm-models:    ## эндпоинты модели: провайдеры и цены: make llm-models MODEL=openai/gpt-5.6-luna
	docker compose run --rm migrate python -m app.cli.llm_models $(MODEL)

target-list:   ## список целевых каналов
	docker compose run --rm migrate python -m app.cli.sources target-list