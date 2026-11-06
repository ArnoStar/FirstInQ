HOST ?= 127.0.0.1
PORT ?= 8080

UVICORN = uv run uvicorn app.main:app --host $(HOST) --port $(PORT)

.DEFAULT_GOAL := help
.PHONY: help install run dev test key clean up down logs

help: ## Show this help
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  make %-8s %s\n", $$1, $$2}'

install: ## Install dependencies
	uv sync

run: .env ## Start the server (HOST/PORT overridable, e.g. make run PORT=9000)
	$(UVICORN)

dev: .env ## Start with auto-reload on code changes
	$(UVICORN) --reload --reload-dir app

test: ## Run the test suite
	uv run pytest

up: .env ## Build and start in Docker (background)
	@docker compose up -d --build --wait --wait-timeout 60 || { \
		echo; docker compose logs --no-log-prefix --tail 10 telegramq | uniq; docker compose stop -t 5 >/dev/null 2>&1; \
		echo; echo "TelegramQ did not start; see the log above. Fix it, then run make up again."; exit 1; }
	@echo "TelegramQ is running at http://$$(docker compose port telegramq 8080)  (make logs / make down)"

down: ## Stop the Docker container
	docker compose down

logs: ## Follow the Docker container logs
	docker compose logs -f

key: ## Print a new SECRET_KEY for .env
	@uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

clean: ## Remove caches (keeps data/ and .env)
	rm -rf .pytest_cache
	find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} +

.env:
	@cp .env.example .env
	@echo "Created .env from .env.example. Fill in TG_API_ID and TG_API_HASH, then run make again."
	@exit 1
