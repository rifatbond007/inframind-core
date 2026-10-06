# InfraMind — developer entry point.
# Run `make help` to see targets.

PY     ?= python3
PIP    ?= $(PY) -m pip
RUFF   ?= $(PY) -m ruff
MYPY   ?= $(PY) -m mypy
PYTEST ?= $(PY) -m pytest
COMPOSE ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show available targets
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} \
		/^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE)

# --- install ---
.PHONY: install
install: ## install package in editable mode with dev extras
	$(PIP) install -e ".[dev]"

.PHONY: precommit
precommit: ## install pre-commit hooks
	$(PY) -m pre_commit install

# --- lint / test ---
.PHONY: lint
lint: ## run ruff check + format check + mypy
	$(RUFF) check src tests
	$(RUFF) format --check src tests
	$(MYPY) src

.PHONY: format
format: ## auto-fix lint issues and format
	$(RUFF) check --fix src tests
	$(RUFF) format src tests

.PHONY: test
test: ## run unit tests
	$(PYTEST) -q

.PHONY: test-all
test-all: ## run all tests including integration
	$(PYTEST) -q -m ""

# --- dev stack ---
# NOTE for Step 0: `make up` brings up only Redis + PostgreSQL via docker-compose.
# In Step 1, this target will additionally bring up the kind-based Kubernetes testbed
# (kind cluster, Online Boutique, kube-prometheus-stack, Loki, Jaeger, Chaos Mesh).
.PHONY: up
up: ## start the local dev stack (Redis + Postgres)
	$(COMPOSE) up -d
	@echo "Stack up. Try: redis-cli -h localhost ping; psql postgresql://inframind:inframind@localhost:5432/inframind"

.PHONY: down
down: ## stop and remove the local dev stack (incl. volumes)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## tail docker compose logs
	$(COMPOSE) logs -f

# --- evaluation / paper (placeholders for later) ---
.PHONY: eval
eval: ## run a single evaluation scenario (not implemented yet — Phase P7)
	@echo "eval target not implemented yet. See docs/PROGRESS.md (Phase P7)."

.PHONY: eval-all
eval-all: ## run the full evaluation matrix (not implemented yet — Phase P7)
	@echo "eval-all target not implemented yet. See docs/PROGRESS.md (Phase P7)."

.PHONY: paper
paper: ## build the paper artifact (not implemented yet — Phase P9)
	@echo "paper target not implemented yet. See docs/PROGRESS.md (Phase P9)."
