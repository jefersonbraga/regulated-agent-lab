# Thin wrappers over the project's own commands (see README "How to run").
# Backend commands run in backend/ (uv project), frontend commands in frontend/ (npm project).

SHELL := /bin/bash
.DEFAULT_GOAL := help

BACKEND  := backend
FRONTEND := frontend
PROD     := docker compose -f infra/docker-compose.prod.yml --env-file infra/.env.prod
# Tests and evals: mock model, test secret (backend/tests/conftest.py sets the same defaults).
TEST_ENV := LLM_PROVIDER=mock AGENT_TOKEN_SECRET=test
# Local API: the repo-root .env when it exists, otherwise a demo setup without login.
ENV_FILE := $(wildcard .env)
DEV_API_ENV := $(if $(ENV_FILE),,API_AUTH_DISABLED=1 LLM_PROVIDER=mock AGENT_TOKEN_SECRET=demo-only)
DEV_API_ARGS := $(if $(ENV_FILE),--env-file ../.env,)

.PHONY: help install dev-api dev-ui test test-backend test-frontend lint format evals evals-live \
	build up down prod-up smoke

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

install: ## Install backend (uv sync) and frontend (npm ci) dependencies
	cd $(BACKEND) && uv sync
	cd $(FRONTEND) && npm ci

dev-api: ## API on http://localhost:8000 (uses ../.env if present, else mock + no login)
	cd $(BACKEND) && $(DEV_API_ENV) uv run uvicorn regulated_agent_lab.api.server:app \
		--no-server-header $(DEV_API_ARGS)

dev-ui: ## Console on http://localhost:5173 (Vite dev server)
	cd $(FRONTEND) && npm run dev

test: test-backend test-frontend ## All tests: backend (unit, integration, evals) and frontend

test-backend: ## Backend tests and scenario evals with the mock model
	cd $(BACKEND) && $(TEST_ENV) uv run pytest -q

test-frontend: ## Frontend typecheck and tests
	cd $(FRONTEND) && npm run typecheck && npm test -- --run

lint: ## ruff check + ruff format --check (backend/ and scripts/)
	cd $(BACKEND) && uv run ruff check . ../scripts && uv run ruff format --check . ../scripts

format: ## ruff format + ruff check --fix (backend/ and scripts/)
	cd $(BACKEND) && uv run ruff format . ../scripts && uv run ruff check --fix . ../scripts

evals: ## Scenario evals only, mock model
	cd $(BACKEND) && $(TEST_ENV) uv run pytest -q evals

evals-live: ## Scenario evals against the real model (needs VLLM_BASE_URL or a cloud API key)
	cd $(BACKEND) && uv run pytest -m live evals -q

build: ## Build both images (compose.yaml)
	docker compose -f compose.yaml build

up: ## Local stack: UI http://localhost:5173, API http://localhost:8000
	docker compose -f compose.yaml up --build

down: ## Stop the local stack (data volume kept; `docker compose down -v` deletes it)
	docker compose -f compose.yaml down

prod-up: ## Production stack (infra/README.md); needs infra/.env.prod
	$(PROD) up -d --build

smoke: ## Smoke test a deployed stack: make smoke DOMAIN=lab-demo.example.org
	@test -n "$(DOMAIN)" || { echo "usage: make smoke DOMAIN=<host>" >&2; exit 2; }
	scripts/smoke_prod.sh $(DOMAIN)
