.PHONY: up down restart logs ps migrate revision psql health test

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