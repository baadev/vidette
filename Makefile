# Vidette developer entry points. Requires: uv (https://docs.astral.sh/uv/), node >= 20.

.PHONY: setup test lint fmt dev web build up down validate

setup:
	cd server && uv sync
	cd web && npm install

test:
	cd server && uv run pytest

lint:
	cd server && uv run ruff check .
	cd server && uv run mypy vidette
	cd web && npm run typecheck

fmt:
	cd server && uv run ruff format .
	cd server && uv run ruff check --fix .

dev:
	cd server && uv run uvicorn vidette.api.app:create_app --factory --reload --port 8642

web:
	cd web && npm run dev

build:
	cd web && npm run build
	docker build -f deploy/Dockerfile -t vidette:local .

up:
	docker compose -f deploy/docker-compose.yml up -d --build

down:
	docker compose -f deploy/docker-compose.yml down

validate:
	cd server && uv run vidette validate ../deploy/config.example.yaml
