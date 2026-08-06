.PHONY: help install dev-install browsers db-up db-down migrate revision run api \
        dashboard test lint fmt typecheck docker-build docker-up docker-down clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package
	pip install -e .

dev-install:  ## Install with dev extras + Playwright browser
	pip install -e ".[dev]"
	python -m playwright install --with-deps chromium

browsers:  ## Install the Playwright Chromium build only
	python -m playwright install chromium

db-up:  ## Start Postgres in Docker
	docker compose up -d postgres

db-down:  ## Stop Postgres
	docker compose stop postgres

migrate:  ## Apply all database migrations
	alembic upgrade head

revision:  ## Autogenerate a migration:  make revision m="add x"
	alembic revision --autogenerate -m "$(m)"

seed:  ## Load niche + location reference data
	leadgen seed

run:  ## Run today's pipeline once
	leadgen run-daily

api:  ## Start the API + dashboard with autoreload
	uvicorn leadgen.api.app:app --reload --host 0.0.0.0 --port 8000

test:  ## Run the test suite
	pytest

test-cov:  ## Run tests with a coverage report
	pytest --cov=leadgen --cov-report=term-missing --cov-report=html

lint:  ## Lint
	ruff check src tests

fmt:  ## Format + autofix
	ruff format src tests
	ruff check --fix src tests

typecheck:  ## Static type check
	mypy src/leadgen

docker-build:  ## Build the application image
	docker compose build

docker-up:  ## Start the full stack
	docker compose up -d

docker-down:  ## Stop the full stack
	docker compose down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
