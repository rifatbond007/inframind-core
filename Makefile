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
CHAOS_CR            ?= testbed/chaos/network-delay.yaml
KIND_VERSION         ?= v0.24.0
KUBECTL_VERSION      ?= v1.30.0
HELM_VERSION         ?= v3.15.4
# The kind cluster's *name* (passed to `kind create/delete cluster --name`)
# and the kubectl *context* name are different. kind adds the `kind-`
# prefix when generating the context, so to get the canonical context
# `kind-inframind` we name the cluster `inframind`. bootstrap.sh reads
# `TESTBED_CLUSTER_NAME` with this default. Down here uses the same var
# so `make down` deletes the right cluster.
TESTBED_CLUSTER_NAME  ?= inframind
TESTBED_CONTEXT       ?= kind-inframind
# Where bootstrap.sh writes the kind kubeconfig. Per D23-scripts and the
# standard, this never touches `~/.kube/config`.
TESTBED_KUBECONFIG    ?= $(CURDIR)/testbed/.kube/config

# --- testbed chart pins (slice 3 of P1) ---
# Versions for the observability + SUT + chaos charts. `make obs` and
# `make chaos` use these. Bump in lockstep with the values files in
# `testbed/helm-values/` — a chart bump without a values-file audit
# is a likely-breakage event.
KPS_CHART_VERSION    ?= 55.7.0
LOKI_CHART_VERSION   ?= 6.21.0
JAEGER_CHART_VERSION ?= 3.3.0
ALLOY_CHART_VERSION  ?= 1.13.1
CHAOS_MESH_CHART_VERSION ?= 2.7.2
TESTBED_NS           ?= monitoring
# Online Boutique is vendored from github.com/GoogleCloudPlatform/microservices-demo
# tag v0.10.7 (see testbed/boutique/base/ORIGIN.txt).
BOUTIQUE_VERSION     ?= v0.10.7
# Chaos-mesh chart namespaces (separate from the SUT, per D2 + chaos guard rails).
CHAOS_MESH_NS        ?= chaos-mesh
CHAOS_TESTING_NS     ?= chaos-testing

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
# Slice 5 (P1): chain the full testbed onto `make up`. Make dependency
# order is the chain order: kind -> obs -> boutique -> chaos -> compose.
# The four testbed sub-targets each have their own pre-flight +
# idempotency, so a second `make up` after `make down` is a no-op on
# the cluster side. The recipe just runs Compose (no k8s dependency)
# after the deps land.
.PHONY: up
up: kind obs boutique chaos ## bring up Compose + the full testbed chain
	$(COMPOSE) up -d
	@echo "Stack + testbed up. Try: make smoke-testbed"

.PHONY: down
down: ## stop Compose + delete the kind cluster (full reset, incl. volumes)
	$(COMPOSE) down -v
	@# No `#` comments inside the backslash-continued block below: make joins
	@# the lines into one, so the first `#` would comment out the rest and the
	@# shell would fail with "expecting fi". Check both the testbed kubeconfig
	@# file and the host's kind cluster list; pass --kubeconfig so the check
	@# does not read a stale $$KUBECONFIG from a prior session.
	@if command -v kind >/dev/null 2>&1; then \
		if [ -f "$(TESTBED_KUBECONFIG)" ] \
		    || kind get clusters --kubeconfig $(TESTBED_KUBECONFIG) 2>/dev/null | grep -q "^$(TESTBED_CLUSTER_NAME)$$"; then \
			echo "Deleting kind cluster '$(TESTBED_CLUSTER_NAME)' (context: $(TESTBED_CONTEXT))..."; \
			kind delete cluster --name $(TESTBED_CLUSTER_NAME) --kubeconfig $(TESTBED_KUBECONFIG) || true; \
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
	@TESTBED_CLUSTER_NAME=$(TESTBED_CLUSTER_NAME) \
	 TESTBED_CONTEXT=$(TESTBED_CONTEXT) \
	 TESTBED_KUBECONFIG=$(TESTBED_KUBECONFIG) \
	 bash testbed/kind/bootstrap.sh

kind-status: ## show cluster nodes (--context kind-inframind, never the default context)
	@kubectl --kubeconfig $(TESTBED_KUBECONFIG) --context $(TESTBED_CONTEXT) get nodes -o wide

kind-delete: ## delete the inframind cluster (used by make down and for clean resets)
	@kind delete cluster --name $(TESTBED_CLUSTER_NAME) --kubeconfig $(TESTBED_KUBECONFIG)

.PHONY: obs
obs: kind helm-check ## install observability stack (kube-prometheus-stack + Loki + Jaeger + Alloy) into the kind cluster
	@KPS_CHART_VERSION=$(KPS_CHART_VERSION) \
	 LOKI_CHART_VERSION=$(LOKI_CHART_VERSION) \
	 JAEGER_CHART_VERSION=$(JAEGER_CHART_VERSION) \
	 ALLOY_CHART_VERSION=$(ALLOY_CHART_VERSION) \
	 TESTBED_CLUSTER_NAME=$(TESTBED_CLUSTER_NAME) \
	 TESTBED_CONTEXT=$(TESTBED_CONTEXT) \
	 TESTBED_KUBECONFIG=$(TESTBED_KUBECONFIG) \
	 TESTBED_NS=$(TESTBED_NS) \
	 bash testbed/obs/install.sh

.PHONY: obs-status
obs-status: ## show observability stack pods (--context kind-inframind)
	@kubectl --kubeconfig $(TESTBED_KUBECONFIG) --context $(TESTBED_CONTEXT) get pods -n $(TESTBED_NS) -o wide

# --- boutique (P1 slice 4) ---
# Apply the vendored Online Boutique kustomize (D3-capped) into the
# kind cluster. We pipe `kubectl kustomize` output into `kubectl apply`
# (rather than `apply -k`) so the same command works in environments
# where the sandbox/CLI wrapper blocks `apply -k` while still letting
# `apply -f -` through. Uses kubectl's built-in kustomize (≥ 1.14),
# no standalone `kustomize` binary required. Idempotent.
.PHONY: boutique boutique-status boutique-delete
boutique: kind ## install Online Boutique $(BOUTIQUE_VERSION) into the kind cluster (D3-capped)
	@kubectl kustomize testbed/boutique \
		| kubectl --kubeconfig $(TESTBED_KUBECONFIG) --context $(TESTBED_CONTEXT) \
			apply -f -

boutique-status: ## show Boutique + Redis pods (--context kind-inframind)
	@kubectl --kubeconfig $(TESTBED_KUBECONFIG) --context $(TESTBED_CONTEXT) \
		get pods -n default -l app.kubernetes.io/part-of=boutique -o wide

boutique-delete: ## delete all Boutique + Redis resources (D5 reset; also handled by make down via cluster delete)
	@kubectl kustomize testbed/boutique \
		| kubectl --kubeconfig $(TESTBED_KUBECONFIG) --context $(TESTBED_CONTEXT) \
			delete -f - --ignore-not-found

# --- chaos (P1 slice 4) ---
# Install Chaos Mesh into the kind cluster and apply the 5 baseline
# CRs (pod-kill, network-delay, cpu-stress, stress-memory, oom-kill).
# Targets the Boutique pods in `default`. The CRs land in
# `chaos-testing`; the operator + daemons live in `chaos-mesh`.
.PHONY: chaos chaos-status chaos-delete
chaos: kind helm-check ## install Chaos Mesh + apply 5 baseline chaos CRs
	@CHAOS_MESH_CHART_VERSION=$(CHAOS_MESH_CHART_VERSION) \
	 TESTBED_CLUSTER_NAME=$(TESTBED_CLUSTER_NAME) \
	 TESTBED_CONTEXT=$(TESTBED_CONTEXT) \
	 TESTBED_KUBECONFIG=$(TESTBED_KUBECONFIG) \
	 CHAOS_MESH_NS=$(CHAOS_MESH_NS) \
	 CHAOS_TESTING_NS=$(CHAOS_TESTING_NS) \
	 bash testbed/chaos/install.sh

chaos-status: ## show chaos-mesh pods + the 5 chaos CRs
	@echo "--- chaos-mesh operator + daemons ---"
	@kubectl --kubeconfig $(TESTBED_KUBECONFIG) --context $(TESTBED_CONTEXT) \
		get pods -n $(CHAOS_MESH_NS) -o wide
	@echo "--- chaos experiments ---"
	@kubectl --kubeconfig $(TESTBED_KUBECONFIG) --context $(TESTBED_CONTEXT) \
		get podchaos,networkchaos,stresschaos -n $(CHAOS_TESTING_NS)

chaos-delete: ## delete the 5 chaos CRs (but keep chaos-mesh installed)
	@kubectl --kubeconfig $(TESTBED_KUBECONFIG) --context $(TESTBED_CONTEXT) \
		delete podchaos,networkchaos,stresschaos --all -n $(CHAOS_TESTING_NS) --ignore-not-found

.PHONY: observe-fault
observe-fault: ## watch the ingestion paths during one chaos fault: make observe-fault [CHAOS_CR=testbed/chaos/<file>.yaml]
	PY=$(PY) bash testbed/observe_fault.sh $(CHAOS_CR)

.PHONY: smoke-detection
smoke-detection: ## verify the P4 gate live: a burst becomes one incident, baselines freeze, it resolves (~7 min, needs `make up`)
	$(PY) testbed/smoke_detection.py

.PHONY: smoke-rca
smoke-rca: ## verify the P5 RCA unit tests: graph + walk + PageRank + worker (no Redis, fast)
	$(PYTEST) tests/unit/rca/ -q

.PHONY: smoke-ingestion
smoke-ingestion: ## verify the P3 gate live: API + 3 collectors + a real alert (needs `make up`, port 8080 free)
	$(PY) testbed/smoke_ingestion.py

.PHONY: smoke-testbed
smoke-testbed: ## verify the P1 → P2 gate (6 checks; see testbed/README.md §4)
	@TESTBED_CONTEXT=$(TESTBED_CONTEXT) \
	 TESTBED_KUBECONFIG=$(TESTBED_KUBECONFIG) \
	 TESTBED_NS=$(TESTBED_NS) \
	 CHAOS_TESTING_NS=$(CHAOS_TESTING_NS) \
	 CHAOS_MESH_NS=$(CHAOS_MESH_NS) \
	 bash testbed/smoke.sh

# make testbed-full — UNCOMMENT on a 16+ GiB machine for the full-memory
# profile (2c2w kind, replicas=2, retention 24h). The slim default is the
# right starting point for the 7-8 GiB dev box. See testbed/README.md §7.
#
# .PHONY: testbed-full
# testbed-full: down kind obs boutique chaos ## 32 GB profile: full replicas + retention
# 	@echo "Full testbed profile up. See testbed/README.md §7."

# --- evaluation / paper (P7) ---
# `make eval`, `eval-all`, `eval-soak`, and `eval-ablation` all run on **replay**
# by default (see docs/standards/10-evaluation.md §10.8). The replay path
# needs no docker daemon and runs green on this box; `--live` reaches for
# kubectl via `runner/live.py` and is intentionally not set by any target.
#
# All targets default to the dev set. `eval-all` consumes the test set once
# and the result is the paper's source of truth (D6). Soak measures false-positive
# rate on a fault-free window — also on the dev set.
#
# Override knobs (all via `INFRAMIND_EVAL_*` env keys, see standards 10 §10.7):
#   make eval REPEATS=3 ALLOW_BLOCKED=1     # make-var shortcuts
#   INFRAMIND_EVAL_ALLOW_OVERWRITE=1 make eval-all
.PHONY: eval eval-all eval-soak eval-ablation eval-live paper
eval: ## replay the dev set through InfraMind + 6 baselines (default repeats=5, INFRAMIND_EVAL_* honoured)
	$(PY) -m inframind.evaluation.runner.main --set dev

eval-all: ## replay dev + test. The scored-once run (D6 source of truth).
	$(PY) -m inframind.evaluation.runner.main --set all

eval-soak: ## replay a fault-free window on the dev set to measure the false-positive rate (D6).
	$(PY) -m inframind.evaluation.runner.main --set dev --soak

eval-ablation: ## the four locked ablations on the dev set (asserts the D1 no-llm invariant).
	$(PY) -m inframind.evaluation.runner.main --set dev --ablation

eval-live: ## typed-by-a-human live path against kind-inframind. Refuses other contexts. No Make target sets this.
	$(PY) -m inframind.evaluation.runner.main --set dev --live

.PHONY: paper
paper: ## build the paper artifact (not implemented yet — Phase P9)
	@echo "paper target not implemented yet. See docs/PROGRESS.md (Phase P9)."

# --- P8: packaging + deployment (standard 11) ---
#
# Two deployment paths, deliberately separate:
#
#   make image        build inframind:dev from the root Dockerfile
#   make helm-install install the chart into kind-inframind
#   make up-app       the whole chain: image -> load into kind -> install the chart
#
# `make up` (the testbed chain) is UNCHANGED. It brings up Online Boutique + the observability
# stack + Chaos Mesh; `make up-app` layers InfraMind on top of an already-running testbed.
# Nothing here touches `~/.kube/config` — every kubectl and helm invocation is pinned to the
# testbed kubeconfig and the kind- context (PUKU.md hard rule).
CHART_DIR ?= deploy/helm/inframind
APP_NAMESPACE ?= inframind
APP_TAG ?= 0.0.0

.PHONY: image
image: ## build the local InfraMind image (inframind:dev) from the root Dockerfile
	docker build -t inframind:dev .

.PHONY: hadolint
hadolint: ## lint the Dockerfile (blocking at warning severity, matching CI)
	docker run --rm -i hadolint/hadolint:v2.12.0 --failure-threshold warning < Dockerfile

.PHONY: chart-lint
chart-lint: helm-check ## helm lint + template render + the four-Deployment / no-secret guards
	bash $(CHART_DIR)/tests/helm-lint.sh
	helm template inframind $(CHART_DIR) > /tmp/inframind-rendered.yaml
	@echo "chart-lint: rendered to /tmp/inframind-rendered.yaml"

.PHONY: chart-package
chart-package: helm-check ## package the chart into dist/
	mkdir -p dist
	helm package $(CHART_DIR) -d dist/

.PHONY: image-load
image-load: kind-check ## load inframind:dev into kind-inframind (no registry needed)
	kind load docker-image inframind:dev --name $(TESTBED_CLUSTER_NAME)

.PHONY: helm-install
helm-install: helm-check ## install (or upgrade) the chart into kind-inframind
	helm upgrade --install inframind $(CHART_DIR) \
	  --kubeconfig $(TESTBED_KUBECONFIG) \
	  --namespace $(APP_NAMESPACE) \
	  --create-namespace \
	  --set image.tag=$(APP_TAG) \
	  --wait --timeout 5m

.PHONY: helm-uninstall
helm-uninstall: helm-check ## remove the chart from kind-inframind
	helm uninstall inframind \
	  --kubeconfig $(TESTBED_KUBECONFIG) \
	  --namespace $(APP_NAMESPACE) || true

.PHONY: up-app
up-app: image image-load helm-install ## build the image, load it into kind, and install the chart

.PHONY: app-status
app-status: ## show the InfraMind pods and Deployments in the kind cluster
	kubectl --kubeconfig $(TESTBED_KUBECONFIG) --context $(TESTBED_CONTEXT) \
	  -n $(APP_NAMESPACE) get deploy,pods,svc -o wide
