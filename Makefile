# InfraMind — developer entry point.
# Run `make help` to see targets.

PY     ?= python3
PIP    ?= $(PY) -m pip
RUFF   ?= $(PY) -m ruff
MYPY   ?= $(PY) -m mypy
PYTEST ?= $(PY) -m pytest
COMPOSE ?= docker compose

# --- testbed tool pins (slice 1 of P1) ---
# Each `make <tool>-check` target verifies the pinned version is on PATH.
# The pinned versions are the known-good set for the chart versions in
# `testbed/helm-values/`. Do not bump without re-running `make smoke-testbed`.
KIND_VERSION         ?= v0.24.0
KUBECTL_VERSION      ?= v1.30.0
HELM_VERSION         ?= v3.15.4
TESTBED_CONTEXT      ?= kind-inframind
# Where bootstrap.sh writes the kind kubeconfig. Per D23-scripts and the
# standard, this never touches `~/.kube/config`.
TESTBED_KUBECONFIG   ?= $(CURDIR)/testbed/.kube/config

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
# Step 0 (Redis + Postgres) lives here. The P1 testbed chain (kind,
# observability, Online Boutique, Chaos Mesh) is added by the P1 slice 5
# commit; see testbed/README.md for the planned order.
.PHONY: up
up: ## start the local dev stack (Redis + Postgres). P1 testbed lands in slice 5.
	$(COMPOSE) up -d
	@echo "Stack up. Try: redis-cli -h localhost ping; psql postgresql://inframind:inframind@localhost:5432/inframind"

.PHONY: down
down: ## stop Compose + delete the kind cluster (full reset, incl. volumes)
	$(COMPOSE) down -v
	@if command -v kind >/dev/null 2>&1; then \
		if [ -f "$(TESTBED_KUBECONFIG)" ] || kind get clusters 2>/dev/null | grep -q "^$(TESTBED_CONTEXT)$$"; then \
			echo "Deleting kind cluster '$(TESTBED_CONTEXT)'..."; \
			kind delete cluster --name $(TESTBED_CONTEXT) --kubeconfig $(TESTBED_KUBECONFIG) || true; \
		fi; \
	else \
		echo "kind not on PATH; skipping cluster teardown. Install it to fully clean up."; \
	fi

.PHONY: logs
logs: ## tail docker compose logs
	$(COMPOSE) logs -f

# --- testbed tooling checks (P1 slice 1) ---
# Each target exits 0 with the installed version, or fails with the install
# command. The pinned version strings live in the variables at the top of
# this Makefile. These targets exist so the runbook acceptance check is
# mechanical: `make kind-check helm-check kubectl-check` all exit 0.
.PHONY: kind-check
kind-check: ## verify kind $(KIND_VERSION) is on PATH
	@if ! command -v kind >/dev/null 2>&1; then \
		echo "kind not found on PATH."; \
		echo "Install:"; \
		echo "  curl -fsSL https://kind.sigs.k8s.io/dl/$(KIND_VERSION)/kind-linux-amd64 -o ~/.local/bin/kind && chmod +x ~/.local/bin/kind"; \
		echo "  (or use 'go install sigs.k8s.io/kind@$(KIND_VERSION)' if you have Go)"; \
		exit 1; \
	fi
	@ver=$$(kind version | awk '{print $$2}'); \
	echo "kind $$ver (expected $(KIND_VERSION))"; \
	if [ "$$ver" != "$(KIND_VERSION)" ]; then \
		echo "WARN: kind version mismatch. Re-install $(KIND_VERSION) for chart compatibility."; \
	fi

.PHONY: kubectl-check
kubectl-check: ## verify kubectl $(KUBECTL_VERSION) is on PATH
	@if ! command -v kubectl >/dev/null 2>&1; then \
		echo "kubectl not found on PATH."; \
		echo "Install:"; \
		echo "  curl -fsSLO https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/linux/amd64/kubectl && install -m 0755 kubectl ~/.local/bin/kubectl"; \
		exit 1; \
	fi
	@ver=$$(kubectl version --client --output=yaml 2>/dev/null | awk '/gitVersion:/{print $$2}' | tr -d '"' | head -1); \
	if [ -z "$$ver" ]; then ver=$$(kubectl version --client --short 2>/dev/null | awk '{print $$3}'); fi; \
	echo "kubectl $$ver (expected $(KUBECTL_VERSION))"; \
	if [ "$$ver" != "$(KUBECTL_VERSION)" ]; then \
		echo "WARN: kubectl version mismatch. Re-install $(KUBECTL_VERSION)."; \
	fi

.PHONY: helm-check
helm-check: ## verify helm $(HELM_VERSION) is on PATH
	@if ! command -v helm >/dev/null 2>&1; then \
		echo "helm not found on PATH."; \
		echo "Install:"; \
		echo "  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash -s -- --version $(HELM_VERSION)"; \
		exit 1; \
	fi
	@ver=$$(helm version --short | sed 's/^v//; s/+.*//'); \
	echo "helm v$$ver (expected $(HELM_VERSION))"; \
	if [ "v$$ver" != "$(HELM_VERSION)" ]; then \
		echo "WARN: helm version mismatch. Re-install $(HELM_VERSION) for chart compatibility."; \
	fi

# --- testbed (P1) ---
# Slice 1 lands the stub for `smoke-testbed`; the full implementation comes
# in slice 5 alongside the `make up` chain. Slices 2-4 land `make kind`,
# `make obs`, `make boutique`, `make chaos` in turn.

.PHONY: kind kind-status kind-delete
kind: kind-check ## create the kind-inframind cluster (idempotent; 1c1w slim default)
	@bash testbed/kind/bootstrap.sh

kind-status: ## show cluster nodes (--context kind-inframind, never the default context)
	@kubectl --kubeconfig $(TESTBED_KUBECONFIG) --context $(TESTBED_CONTEXT) get nodes -o wide

kind-delete: ## delete the kind-inframind cluster (used by make down and for clean resets)
	@kind delete cluster --name $(TESTBED_CONTEXT) --kubeconfig $(TESTBED_KUBECONFIG)

.PHONY: smoke-testbed
smoke-testbed: ## verify the P1 → P2 gate (lands fully in slice 5 of P1)
	@echo "smoke-testbed is not yet implemented."
	@echo "It will land in P1 slice 5 with the make up chain."
	@echo "For now, see testbed/README.md §4 for the planned gate checks."
	@exit 1

# make testbed-full — UNCOMMENT on a 16+ GiB machine for the full-memory
# profile (2c2w kind, replicas=2, retention 24h). The slim default is the
# right starting point for the 7-8 GiB dev box. See testbed/README.md §7.
#
# .PHONY: testbed-full
# testbed-full: down kind obs boutique chaos ## 32 GB profile: full replicas + retention
# 	@echo "Full testbed profile up. See testbed/README.md §7."

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
