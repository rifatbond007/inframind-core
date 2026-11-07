# P0–P3 End-to-End Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every finished phase (P0 repo/tooling, P1 testbed, P2 schema + bus, P3 ingestion) pass its gate reproducibly, in CI and on a fresh machine, with the docs, agents and skills agreeing with the code.

**Architecture:** Fix the things that break trust first (CI lint parity, wrong ownership metadata), then close the correctness gaps found in the P3 collectors (restart replay, redaction, hostile webhook input, duplicated entrypoint code), then add a one-command live gate for P3 and a safe fault-observation script, and last sync the standards, agents and skills to what was built.

**Tech Stack:** Python 3.11+, pydantic v2, redis-py asyncio, httpx, FastAPI, pytest + fakeredis, ruff, mypy, pre-commit, kind/kubectl/helm, Chaos Mesh.

**Spec:** There is no brainstormed spec for this work. The sources of truth are `docs/standards/` (esp. 01, 03, 04, 08, 13), `docs/decision-tree.md` (D1–D25), `CLAUDE.md`, `.claude/skills/phase-gates/SKILL.md` (gates P0→P1 … P3→P4) and the agent files under `.claude/agents/`. Each task names the clause it implements.

## Global Constraints

- Language: code, comments, commit messages, docs in English only.
- Python `>=3.11`; ruff line length 100, rules `E,F,W,I,B,UP,SIM,RUF`; `mypy src` clean; pytest `--strict-markers`, `asyncio_mode=auto`.
- Module DAG (standards 01 §1.3): `common` imports nothing from InfraMind; `ingestion`→`common`; `rca`→`common` only; `api`→`common, storage, alerting` and is the only FastAPI importer. No new top-level directory without a D-number.
- Branch `dev-rifat` only (fixed branch set). Conventional Commits; author `user.name` equals the local part of a `<username>@users.noreply.github.com` email; **no `Co-Authored-By:` AI trailer** (D25) — the session attribution reminder does not apply to this repo.
- `kubectl`/`helm` only against `kind-*` contexts; kubeconfig stays in `testbed/.kube/config`.
- Never `--no-verify`, never force-push, never push to `main`, never commit `.env` or secrets (the dev webhook token `dev-secret` is a documented placeholder only).
- Paper/eval numbers come only from `evaluation/results/`; nothing in this plan produces paper numbers.
- Every task ends with `make lint` and `make test` green (run through the repo `.venv` once Task 1 repairs it; until then via the scratch venv, see Task 1 Step 1).
- Stream names are pinned: `stream:signals`, `stream:incidents`, `stream:graph-updates`. Webhook route is `POST /webhooks/alertmanager`.
- Service names are lower-kebab (`re.sub(r"[^a-z0-9-]", "-", name.lower())`); edges are `caller -> callee` (D2).

## Findings this plan fixes

| # | Finding (evidence) | Task |
|---|---|---|
| F1 | CI lint installs unpinned `ruff>=0.4` while pre-commit pins `v0.4.10`; current ruff (0.17.1) reports **11 import-order errors** on the committed tree, so CI lint fails | 1 |
| F2 | `.venv` is Python 3.14 with no pip and no deps; `make install` / `make test` do not work on this machine | 1, 12 |
| F3 | `CODEOWNERS` uses `@moneem`, `@rifatbroh`, `@inframind-team`; D25 usernames are `rifatbond007`, `moneem-07`, `promerayhan`, and a user-owned repo has no teams. `pyproject` Homepage points at `BAIUST-CSE/inframind` but `origin` is `rifatbond007/inframind-core`. Agent `model: opus 4.8` is not a model ID (`claude-opus-4-8` is) | 2 |
| F4 | Module DAG is "enforced by code review" only; no test | 3 |
| F5 | Three collectors copy the same `run`/`amain` signal-handling code; `amain` is untested; Prometheus cursor dict grows with pod churn | 4 |
| F6 | Collector restart replays up to 60–120 s of Loki/Jaeger data (in-memory cursors/seen-sets); duplicate `GraphUpdate`s inflate the RCA call counts | 5 |
| F7 | Standard 03 §3.4 requires redaction in the collector; none exists. Loki messages, Jaeger status descriptions and alert annotations flow to the bus raw | 6 |
| F8 | Webhook reads an unbounded body; a partial publish then 503 makes Alertmanager resend the whole group | 7 |
| F9 | `service_from_pod` mis-strips control-plane static pods: `etcd-inframind-control-plane` -> `etcd-inframind`, `kube-apiserver-inframind-control-plane` -> `kube-apiserver-inframind` (`-control-plane` looks like a hash + 5-char suffix). Confirmed by running the function. The live etcd alert is attributed to a service that does not exist | 8 |
| F10 | The P3 gate was verified by hand; there is no repeatable live check | 9 |
| F11 | `kubectl delete` of the NetworkChaos CR blocked ~7 min on a finalizer, so a delete-then-apply chaos run never injected during its window | 10 |
| F12 | Standards 03/04/11, `surface-map.md`, three READMEs/docstrings, `ingestion-engineer.md`, `alerting-storage-engineer.md` describe names/routes/behaviour that no longer match the code (list in Task 11) | 11 |
| F13 | `.env.example` (uncommitted edit) points at ports 9090/3100/16686/9093; the testbed publishes 30090/30100/30686/30093; new env keys are undocumented | 2 |

## Review Focus

1. **Collector restart replays data** — a restarted Loki or Jaeger collector must not republish what the previous process already published (Task 5 test `test_restarted_collector_does_not_republish`).
2. **Hostile or oversized webhook body** — a >1 MiB body, invalid UTF-8, a non-object, or 10 000 alerts must be rejected with a 4xx and publish nothing (Task 7).
3. **Secrets/PII in log lines, span descriptions and annotations** — JWTs, bearer tokens, emails, IPs and `password=…` pairs must not reach the bus (Task 6).
4. **Alertmanager retry after partial publish** — a bus failure mid-group returns 503 and Alertmanager resends the group; retried signals must carry the same `fingerprint`/`starts_at` so downstream can dedupe (Task 7).
5. **Non-Boutique pod names** — control-plane static pods and DaemonSet pods must not be stripped to or kept as bogus services (Task 8; the first confirmed bug).

---

### Task 1: Lint parity between pre-commit, CI and local (F1, F2)

**Files:**
- Modify: `pyproject.toml` (`[tool.ruff]`, `[tool.ruff.lint.isort]`, dev extras), `.pre-commit-config.yaml` (ruff `rev`), `.github/workflows/ci.yml` (lint job install), every file ruff re-sorts under `src/` and `tests/`
- Test: none new; the check is that two independent ruff entry points agree

**Interfaces:**
- Produces: one ruff version used by dev extras, pre-commit and CI; `inframind` classified first-party everywhere.

- [ ] **Step 1: Reproduce the CI failure.** Use the scratch venv (`/tmp/claude-1000/-home-rifatbroh-projects-infraMind/a8814417-e428-4a2d-8c04-8ab8c44c297a/scratchpad/verify-venv/bin/python`) as `$V`. Run `$V -m ruff check src tests`. Expected: `Found 11 errors` (all `I001`).
- [ ] **Step 2: Pin one version.** Set `ruff==0.17.1` in the `dev` extra (the version already used above), set the `astral-sh/ruff-pre-commit` `rev` to `v0.17.1` (confirm the tag with `git ls-remote --tags https://github.com/astral-sh/ruff-pre-commit v0.17.1`), and add `src = ["src"]` under `[tool.ruff]` plus `known-first-party = ["inframind"]` under `[tool.ruff.lint.isort]` so classification does not depend on version defaults.
- [ ] **Step 3: Make CI lint install the same environment.** In the lint job replace `pip install "ruff>=0.4" "mypy>=1.10"` with `pip install -e ".[dev]"` so `mypy src` sees pydantic/redis/fastapi types exactly as locally.
- [ ] **Step 4: Re-sort and reformat once.** `$V -m ruff check --fix src tests && $V -m ruff format src tests`; review `git diff --stat` (import-order and formatting changes only, no logic).
- [ ] **Step 5: Verify parity.** `pre-commit run --all-files` and `$V -m ruff check src tests && $V -m ruff format --check src tests && $V -m mypy src` all exit 0 on the same tree; `$V -m pytest -q -m ""` still passes.
- [ ] **Step 6: Commit** `chore(repo): pin ruff and align pre-commit, CI and local lint`.

---

### Task 2: Ownership metadata, agent model IDs and `.env.example` (F3, F13)

**Files:**
- Modify: `.github/CODEOWNERS`, `pyproject.toml` (`[project.urls]`), `.claude/agents/**/*.md` (`model:` line only), `.env.example`
- Test: `tests/unit/test_repo_metadata.py`

**Interfaces:**
- Produces: `tests/unit/test_repo_metadata.py` asserting repo-level invariants (reused by Task 11).

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_repo_metadata.py`:
  - `test_codeowners_handles_are_github_usernames`: every `@handle` in `.github/CODEOWNERS` is in `{"@rifatbond007", "@moneem-07", "@promerayhan"}`.
  - `test_agent_model_ids_are_valid`: every `model:` front-matter value under `.claude/agents` matches `^claude-[a-z0-9-]+$`.
  - `test_env_example_urls_match_testbed_nodeports`: `.env.example` has `PROMETHEUS_URL=http://localhost:30090`, `LOKI_URL=http://localhost:30100`, `JAEGER_URL=http://localhost:30686`, and defines `INFRAMIND_ALERT_WEBHOOK_SECRET`, `PROM_INTERVAL_S`, `LOKI_INTERVAL_S`, `JAEGER_INTERVAL_S`, `INFRAMIND_API_PORT`.
  - `test_pyproject_homepage_matches_git_origin_name`: Homepage ends with `/inframind-core`.
- [ ] **Step 2: Run** `pytest tests/unit/test_repo_metadata.py -q` → 4 failures.
- [ ] **Step 3: Fix the files.** CODEOWNERS: `/src/inframind/ingestion/ detection/ correlation/` → `@moneem-07`; `rca/ llm/ alerting/ storage/ api/` → `@promerayhan`; testbed/deploy/Makefile/compose/Dockerfile/evaluation → `@rifatbond007`; default `/*` → all three handles (no team). Homepage `https://github.com/rifatbond007/inframind-core`. Replace `model: opus 4.8` with `model: claude-opus-4-8` in all 19 agents. In `.env.example` the user's uncommitted edit is **discarded** in favour of the NodePort URLs, with `ALERTMANAGER_URL=http://localhost:30093` kept separate from the webhook secret key; ask the user before overwriting their edit (see Task 12).
- [ ] **Step 4: Run** the new tests and `make lint`/`make test` → PASS.
- [ ] **Step 5: Commit** `chore(repo): fix CODEOWNERS handles, agent model IDs and env template`.

---

### Task 3: Enforce the module DAG with a test (F4)

**Files:**
- Create: `tests/unit/test_module_dag.py`

**Interfaces:**
- Produces: `ALLOWED: dict[str, set[str]]` in the test, transcribed from standards 01 §1.3.

- [ ] **Step 1: Write the test.** `test_module_imports_respect_the_dag` parses every `src/inframind/<pkg>/**/*.py` with `ast`, collects the `inframind.<other>` / relative imports, and asserts each `<other>` is in `ALLOWED[pkg] | {pkg}`. `ALLOWED = {"common": set(), "ingestion": {"common"}, "detection": {"common"}, "correlation": {"common","detection"}, "rca": {"common"}, "llm": {"common","rca"}, "alerting": {"common","rca","llm"}, "storage": {"common","correlation","rca"}, "api": {"common","storage","alerting"}}`. Add `test_only_api_imports_fastapi`: no file outside `src/inframind/api/` contains `import fastapi`/`from fastapi`.
- [ ] **Step 2: Run** `pytest tests/unit/test_module_dag.py -q`. Expected: PASS on the current tree (`api` imports only `common`). If it fails, fix the import, not the table.
- [ ] **Step 3: Sanity-check the test bites.** Temporarily add `from inframind.ingestion import prometheus` to `src/inframind/api/app.py`, confirm the test FAILS, revert.
- [ ] **Step 4: Commit** `test(common): enforce the module dependency DAG`.

---

### Task 4: Shared collector runner and bounded Prometheus cursor (F5)

**Files:**
- Create: `src/inframind/ingestion/runner.py`
- Modify: `src/inframind/ingestion/prometheus.py`, `loki.py`, `jaeger.py` (replace each `amain` body), `prometheus.py` (`_cursor` pruning)
- Test: `tests/unit/ingestion/test_runner.py`, add to `tests/unit/ingestion/test_prometheus.py`

**Interfaces:**
- Produces: `class Collector(Protocol): async def run(self, stop: asyncio.Event) -> None`, and
  `async def run_collector(build: Callable[[httpx.AsyncClient, Redis, int], Collector], *, url_env: str, default_url: str, interval_env: str, timeout_s: float = 15.0, stop: asyncio.Event | None = None) -> None`. When `stop` is None it installs SIGINT/SIGTERM handlers; it always closes the Redis client and the httpx client.
- Consumes: each collector's existing constructor `(client, redis, *, interval_s=…)`.

- [ ] **Step 1: Write failing tests** in `test_runner.py`: `test_runner_builds_with_env_values_and_stops` (monkeypatch `LOKI_URL`, `LOKI_INTERVAL_S=7`; a fake collector records the base URL and interval; `stop` is set after the first `run` call; assert both clients closed), `test_runner_defaults_apply_when_env_is_unset`. In `test_prometheus.py`: `test_cursor_entries_for_vanished_pods_are_pruned` — publish a sample for pod A, advance the clock past `10 * interval_s` with only pod B returned, assert `len(collector._cursor) == 1`.
- [ ] **Step 2: Run** → FAIL (`runner` missing; cursor keeps A).
- [ ] **Step 3: Implement** `runner.py` per the interface; reduce each `amain` to a `run_collector(...)` call (keep `if __name__ == "__main__": asyncio.run(amain())`). Prune `_cursor` entries older than `10 * interval_s` at the end of `poll_once`.
- [ ] **Step 4: Run** `pytest tests/unit/ingestion -q` and `make lint` → PASS; run `python -m inframind.ingestion.loki` for 5 s against the live testbed and Ctrl+C: exits 0 with a `collector_started` line.
- [ ] **Step 5: Commit** `refactor(ingestion): share the collector runner and bound the Prometheus cursor`.

---

### Task 5: Restart-safe collectors (F6)

**Files:**
- Create: `src/inframind/common/collector_state.py`
- Modify: `src/inframind/ingestion/loki.py`, `jaeger.py`
- Test: `tests/unit/common/test_collector_state.py`, additions to `test_loki.py` and `test_jaeger.py`

**Interfaces:**
- Produces:
  ```python
  class CollectorState:
      def __init__(self, client: Redis, name: str, *, ttl_s: int = 3600) -> None: ...
      async def load_cursor(self) -> int | None
      async def save_cursor(self, value: int) -> None
      async def mark_seen(self, key: str) -> bool   # True only the first time (SET NX EX)
  ```
  Keys: `cursor:{name}` and `seen:{name}:{key}`. Redis failures raise `BusError`.
- Consumes: `LokiCollector(..., state: CollectorState | None = None)` and `JaegerCollector(..., state=None)`; `None` keeps today's in-memory behaviour so existing tests are unchanged.

- [ ] **Step 1: Write failing tests.** `test_collector_state.py`: cursor round-trips; `mark_seen` is True then False; a `ConnectionError` raises `BusError`. In `test_loki.py` and `test_jaeger.py`: `test_restarted_collector_does_not_republish` — collector A (shared fakeredis, `CollectorState(redis, "loki")`) publishes N>0 items; a brand-new collector B with the same state and the same fake backend publishes 0.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `CollectorState`; in each collector load the cursor lazily on the first `poll_once`, replace the in-memory seen-set lookups with `mark_seen` **after** a successful publish (never before — a failed publish must stay retryable), and `save_cursor` only when the poll was fully published. Keep the in-memory path when `state is None`. Wire `state=CollectorState(redis, "loki"|"jaeger")` in the `amain` builders.
- [ ] **Step 4: Run** unit tests; then live: run the Jaeger collector for one poll, kill it, restart it, and confirm `XLEN stream:graph-updates` does not grow on the second start (traffic stopped).
- [ ] **Step 5: Commit** `feat(ingestion): persist Loki and Jaeger cursors so restarts do not replay`.

---

### Task 6: Redaction at the collector boundary (F7, standard 03 §3.4, D9)

**Files:**
- Create: `src/inframind/common/redact.py`
- Modify: `src/inframind/ingestion/loki.py` (message), `src/inframind/ingestion/jaeger.py` (status description), `src/inframind/api/alertmanager.py` (annotation values)
- Test: `tests/unit/common/test_redact.py`, additions to the three collector test files

**Interfaces:**
- Produces: `def redact(text: str) -> str` and `def redact_mapping(values: dict[str, Any]) -> dict[str, Any]` (redacts string values, leaves other types and keys). Placeholders: `[REDACTED:jwt]`, `[REDACTED:bearer]`, `[REDACTED:email]`, `[REDACTED:ip]`, `[REDACTED:secret]`.
- Ownership note: `llm/` will reuse these in the explainer (D9); the module lives in `common/` so both can import it without breaking the DAG.

- [ ] **Step 1: Write failing tests** in `test_redact.py` with exact inputs/outputs:
  - `redact("auth Bearer abc.def-123") == "auth [REDACTED:bearer]"`
  - a three-segment `eyJ…` JWT → `[REDACTED:jwt]`
  - `redact("contact a.b@example.com now") == "contact [REDACTED:email] now"`
  - `redact("dial tcp 10.244.1.12:5432 refused") == "dial tcp [REDACTED:ip]:5432 refused"`
  - `redact("password=hunter2 token: abc123 api_key=XYZ")` → every value replaced, keys kept (`password=[REDACTED:secret]`)
  - `redact("could not retrieve product: rpc error: code = NotFound")` unchanged (evidence must survive)
  - idempotent: `redact(redact(s)) == redact(s)`
  Add one test per collector asserting a secret in the source field is absent from `Signal.model_dump_json()`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with precompiled regexes, JWT and bearer matched before email/IP/secret so tokens are not partially rewritten. Apply `redact` to Loki `message`, Jaeger `status_description`, and `redact_mapping` to Alertmanager `annotations` (not `labels`, which carry service identity).
- [ ] **Step 4: Run** `pytest -q` and `make lint` → PASS. Live: produce the frontend error (`/product/NOPE`) and confirm the stored message still contains `code = NotFound`.
- [ ] **Step 5: Commit** `feat(common): redact secrets and PII before collector output reaches the bus`.

---

### Task 7: Harden the Alertmanager webhook (F8)

**Files:**
- Modify: `src/inframind/api/alertmanager.py`
- Test: additions to `tests/unit/api/test_alertmanager.py`

**Interfaces:**
- Produces: constants `MAX_BODY_BYTES = 1_048_576` and `MAX_ALERTS = 1000` in `inframind.api.alertmanager`.

- [ ] **Step 1: Write failing tests:**
  - `test_oversized_body_is_413_and_publishes_nothing` (body of `MAX_BODY_BYTES + 1` bytes with valid auth)
  - `test_too_many_alerts_is_422` (`MAX_ALERTS + 1` alerts)
  - `test_invalid_utf8_body_is_422`
  - `test_unauthorised_oversized_body_is_401_not_413` (auth is checked first)
  - `test_retry_after_bus_failure_yields_identical_fingerprints`: first POST against a failing bus → 503; second POST of the same payload against a working bus → every published signal's `attrs["fingerprint"]` and `attrs["starts_at"]` equal the payload's.
- [ ] **Step 2: Run** → FAIL on the first three.
- [ ] **Step 3: Implement** the size check on `Content-Length` and on the read body, the alert-count check in `alerts_to_signals`, and decode errors mapped to 422. No behaviour change to auth order.
- [ ] **Step 4: Run** `pytest tests/unit/api -q` and `make lint` → PASS.
- [ ] **Step 5: Commit** `fix(api): bound webhook input size and alert count`.

---

### Task 8: Fix `service_from_pod` for static and control-plane pods (F9)

**Files:**
- Modify: `src/inframind/common/signal.py` (`service_from_pod`), fixtures that use made-up pod suffixes (`tests/unit/ingestion/test_prometheus.py`, `tests/unit/ingestion/test_loki.py`, `tests/unit/api/test_alertmanager.py`)
- Test: `tests/unit/common/test_signal.py`

**Interfaces:**
- Produces: unchanged signature `service_from_pod(pod: str) -> str`. New contract: a ReplicaSet pod name is `<service>-<hash>-<suffix>` where `<hash>` is 6–10 and `<suffix>` is exactly 5 characters drawn from Kubernetes' name-generation alphabet `bcdfghjklmnpqrstvwxz2456789` (no vowels, no `0 1 3`); StatefulSet pods end in `-<digits>`. Only those two shapes are stripped. DaemonSet pods (`kindnet-ab12c`, `kube-proxy-x7k2m`) and static pods (`<component>-<node-name>`) are returned normalised, unstripped.

- [ ] **Step 1: Write the failing tests** (parametrised, exact values): `("etcd-inframind-control-plane", "etcd-inframind-control-plane")`, `("kube-apiserver-inframind-control-plane", "kube-apiserver-inframind-control-plane")`, `("kindnet-ab12c", "kindnet-ab12c")`, `("kube-proxy-x7k2m", "kube-proxy-x7k2m")`, and keep green: `("coredns-7db6d8ff4d-5lvfp", "coredns")`, `("redis-cart-c4754f644-2l69d", "redis-cart")`, `("app-v2-5c744dd6bb-jfpxt", "app-v2")`, `("kafka-0", "kafka")`.
- [ ] **Step 2: Run** `pytest tests/unit/common/test_signal.py -q` → the two control-plane cases FAIL.
- [ ] **Step 3: Implement** by tightening `_DEPLOYMENT_POD` to `^(?P<service>.+)-[bcdfghjklmnpqrstvwxz2456789]{6,10}-[bcdfghjklmnpqrstvwxz2456789]{5}$`. Update the test fixtures that used invented suffixes such as `abcde` or `x2k4p` to real-alphabet suffixes (`jfpxt`, `lk9tz`).
- [ ] **Step 4: Run** `pytest -q -m ""` and `make lint` → PASS. Re-run the live etcd alert shape through `alerts_to_signals` (fixture copy of the real alert) and assert `service == "etcd-inframind-control-plane"`.
- [ ] **Step 5: Commit** `fix(common): stop stripping control-plane and DaemonSet pod names to bogus services`.

---

### Task 9: One-command live gate for P3 (F10)

**Files:**
- Create: `testbed/smoke_ingestion.py`
- Modify: `Makefile` (new target `smoke-ingestion`), `testbed/README.md` (gate table row)

**Interfaces:**
- Produces: `python testbed/smoke_ingestion.py` exits 0 only if all checks pass; `make smoke-ingestion` runs it with `$(PY)`. Checks, each printing `OK|FAIL Cn …` in the style of `testbed/smoke.sh`:
  - C1 reachability of Redis, Prometheus (30090), Loki (30100), Jaeger (30686), Alertmanager (30093).
  - C2 it starts the API on port 8080 (the port the in-cluster Alertmanager receiver targets; secret `dev-secret`, matching the committed testbed values) and the three collectors as subprocesses and always kills them on exit.
  - C3 it drives traffic through `kubectl port-forward svc/frontend` including `GET /product/NOPE-<run id>`.
  - C4 within 90 s, on the real pinned streams: Prometheus signals for ≥ 6 distinct services; a Loki error Signal for `frontend` with `attrs["grpc_code"] == "NotFound"`; `GraphUpdate`s covering `frontend->productcatalogservice`, `frontend->recommendationservice`, `recommendationservice->productcatalogservice`; a Jaeger error Signal whose `trace_id` is 32-hex.
  - C5 a synthetic alert posted to the real Alertmanager `/api/v2/alerts` (labels `alertname=InframindSmoke`, `pod=<current frontend pod>`) arrives as an Alertmanager Signal for `frontend` within 90 s. The script fails fast with a clear message if port 8080 is already taken.

- [ ] **Step 1: Write the checker's pure parts first with tests** in `tests/unit/test_smoke_ingestion.py`: `edges_present(updates) -> set[tuple[str,str]]`, `distinct_services(signals, source) -> set[str]`, and `evaluate(signals, updates) -> list[CheckResult]` over fixture lists (a passing set and a set missing one edge → that check FAILs with the missing edge named).
- [ ] **Step 2: Run** → FAIL; implement the pure functions; run → PASS.
- [ ] **Step 3: Implement the live driver** around those functions (subprocess management, `httpx`, redis) and the Makefile target.
- [ ] **Step 4: Run** `make smoke-ingestion` against the live testbed → `5/5 checks passed`. Prove the gate can fail: scale the `jaeger` deployment in `monitoring` to 0, re-run, and confirm C4 FAILS naming the three missing edges; scale it back to 1 and re-run to PASS.
- [ ] **Step 5: Commit** `feat(testbed): add smoke-ingestion, the repeatable P3 live gate`.

---

### Task 10: Fault-observation run that cannot hang (F11)

**Files:**
- Create: `testbed/observe_fault.sh`
- Modify: `testbed/README.md` (usage), `Makefile` (`observe-fault` target, `CHAOS_CR` variable defaulting to `testbed/chaos/network-delay.yaml`)

**Interfaces:**
- Produces: `bash testbed/observe_fault.sh <cr-file> [baseline_s=90] [fault_s=150] [recovery_s=90]` writes `testbed/.observe/<run-id>/{markers.env,signals.ndjson,updates.ndjson}` (gitignored) and prints a per-30 s timeline table (edge latency, per-service p95, error counts, CPU, alerts) marking the fault window.
- Rule: never `delete`/re-`apply` a CR by its repo name. The script copies the CR with `metadata.name` suffixed by the run id, creates it, and removes it with `kubectl delete --wait=false` at the end of `fault_s`, then waits for `desiredPhase`/object absence with a 120 s cap and reports (does not hang) if the finalizer is slow.

- [ ] **Step 1: Write a failing test** for the pure timeline builder in `tests/unit/test_observe_fault.py` (module `testbed/observe_fault_report.py`, importable as a script helper): given NDJSON fixtures and markers, `build_timeline(signals, updates, markers) -> str` shows the `recommendationservice->productcatalogservice` row jumping from ~1 ms to ~200 ms inside the fault columns only.
- [ ] **Step 2: Run** → FAIL; implement; run → PASS.
- [ ] **Step 3: Implement the shell driver** (collectors + API + traffic as in Task 9, unique CR name, `--wait=false`, trap-based cleanup that also removes the run's CR).
- [ ] **Step 4: Run** `make observe-fault` once for the network-delay CR (~6 min). Expected: the edge latency rises to the 150–300 ms range during the fault window and falls back after; record the table in the PROGRESS row. If nothing rises, treat that as a defect in the collectors or the CR, not as an acceptable result — diagnose before continuing.
- [ ] **Step 5: Commit** `feat(testbed): add observe-fault with unique CR names and a non-blocking cleanup`.

---

### Task 11: Sync standards, agents, skills and READMEs to the code (F12)

Per the `phase-gates` rule, a change to a standard and to the agent that owns the code ships together.

**Files:**
- Modify: `docs/standards/03-signal-schema.md` (§3.6 `service` from `pod`, §3.7 resolution order `deployment → pod → service → job`, all alerts per group, `fromisoformat`), `docs/standards/04-add-collector.md` (real names: `stream_keys.SIGNALS`, `bus.publish`, async, field `data`, Redis container commands, the three real collectors, `run_collector`, Boutique-appropriate queries), `docs/standards/11-helm-packaging.md:79` (webhook path), `docs/standards/13-service-graph-contract.md` (§13.2 `GraphUpdate` is pydantic in `common/graph_update.py`), `docs/surface-map.md` (Signal block, webhook module path), `docs/decision-tree.md` (change-log row for the service-resolution order — it deviates from standard 03), `src/inframind/ingestion/README.md`, `src/inframind/ingestion/__init__.py`, `src/inframind/api/README.md`, `src/inframind/api/__init__.py` (route `/webhooks/alertmanager`), `src/inframind/common/README.md` (list the new modules), `.claude/agents/build/ingestion-engineer.md` (collector table: `prometheus.py`, `loki.py`, `jaeger.py`; webhook lives in `api/alertmanager.py`; drop `alertmanager_webhook.py` and `otel.py`), `.claude/agents/build/alerting-storage-engineer.md:16`, `.claude/skills/add-collector/SKILL.md` and `.claude/skills/signal-schema/SKILL.md` (pointers only), `CLAUDE.md` (commands `make smoke-ingestion`, `make observe-fault`, collector entrypoints, env keys).
- Test: extend `tests/unit/test_repo_metadata.py`

**Interfaces:** none.

- [ ] **Step 1: Write failing tests:** `test_docs_do_not_reference_removed_names` fails if any file under `docs/standards`, `docs/surface-map.md`, `.claude`, `src/**/README.md` or `src/**/__init__.py` contains `SIGNALS_STREAM`, `get_redis`, `redis_client`, `inframind.signals`, `alertmanager_webhook`, `api.webhooks`, `from dateutil`, or the route `"/alertmanager"` without the `/webhooks` prefix. `test_ingestion_agent_lists_only_existing_collectors` checks every `*.py` named in the agent's collector table exists under `src/inframind/ingestion/` or `src/inframind/api/`.
- [ ] **Step 2: Run** → FAIL listing each stale reference (this list is the work).
- [ ] **Step 3: Edit each file** until the tests pass. Do not rewrite sections that are still true; change only names, routes, signatures and the deviations listed above.
- [ ] **Step 4: Run** `pytest -q`, `make lint`, `pre-commit run --all-files` → PASS.
- [ ] **Step 5: Commit** `docs(standards): sync collector, webhook and graph docs and agents with the code`.

---

### Task 12: Machine and repository state that only the user can change (F2, F3)

These steps are **not automated**; each needs the user and must be confirmed before it runs.

- [ ] **Step 1: Repair the venv.** The distro Python has no `ensurepip`. User runs `! sudo apt install python3-venv` (or installs `uv`), then `rm -rf .venv && python3 -m venv .venv && . .venv/bin/activate && make install && make precommit`. Verify `make lint && make test` run **natively** (no scratch venv) and `make test-all` passes with the compose Redis up.
- [ ] **Step 2: Decide `.env.example`.** Ask the user whether to discard their uncommitted 9090/3100/… edit (Task 2 assumes yes) and then commit `CLAUDE.md` + `.env.example`.
- [ ] **Step 3: Push.** `git push origin dev-rifat` (user confirms); open the PR `dev-rifat → dev` so Moneem (ingestion owner) can review.
- [ ] **Step 4: GitHub settings.** Branch protection on `main` (PR-only, no force-push, `lint` and `test` required, linear history) — the one P0 gate item that lives outside the repo.
- [ ] **Step 5: From-scratch testbed check.** With explicit confirmation (destroys the cluster): `make down && make up && make smoke-testbed && make smoke-ingestion`; record both results in `docs/PROGRESS.md`. This closes the P1 "fresh `make down && make up` re-check pending" note.

---

### Task 13: Gate verification and progress log

**Files:**
- Modify: `docs/PROGRESS.md`

- [ ] **Step 1: Run every applicable gate** and paste the outputs' summary lines into the PROGRESS row: `pre-commit run --all-files`, `make lint`, `make test`, `make test-all`, `make smoke-testbed` (6/6), `make smoke-ingestion` (5/5), `make observe-fault` timeline.
- [ ] **Step 2: Walk the `phase-gates` checklist** for P0→P1, P1→P2, P2→P3 and P3→P4 line by line; each line gets `done (evidence)` or `open (reason)`. Anything still open after Task 12 stays open — do not tick it.
- [ ] **Step 3: Update the PROGRESS row and phase line,** commit `docs(progress): record the P0-P3 hardening results`.

---

## Self-review

- **Spec coverage:** P0 gate (CI, compose, PROGRESS, repo, CODEOWNERS) → Tasks 1, 2, 12; P1 gate → Tasks 10, 12 (fresh re-check); P2 gate (Signal model, idempotent bus, tests) → Tasks 5, 8, and docs in 11; P3 gate (Prometheus+Alertmanager, Loki+Jaeger, `GraphUpdate`) → Tasks 4–10; phase-gates agent-sync rule → Task 11; D9 → Task 6; DAG rule → Task 3.
- **Type consistency:** `CollectorState` (Task 5) is the only new constructor argument consumed later (Task 4's `run_collector` builders pass it in Task 5); `redact`/`redact_mapping` (Task 6) are used by name in Tasks 6 and 11; `MAX_BODY_BYTES`/`MAX_ALERTS` only inside Task 7; `evaluate(...)` and `build_timeline(...)` are defined in the task that tests them.
- **Ordering risk:** Task 1 changes formatting across many files, so it goes first and alone; Task 4 changes the `amain` builders that Task 5 extends, so 4 precedes 5.
- **Known open items after this plan:** a real chaos fault that triggers a default Alertmanager rule (rules have long `for:` delays), P4 and later phases, and anything in Task 12 the user declines.
