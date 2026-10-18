# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# InfraMind — Project Memory (read this first, every session)

**InfraMind**: Kubernetes multi-signal incident detection + graph-guided root cause analysis (RCA).
BSc capstone, BAIUST CSE. Supervisor: Golam Moktader Nayeem. Goal: working prototype -> evaluation -> thesis report -> research paper.

One-line behaviour: when a fault hits a Kubernetes microservice app, InfraMind says **which service is the root cause, with evidence**, and sends one deduplicated alert.

## Language policy
- Talk to the user in the language they write (Bangla/Banglish is fine).
- Code, comments, commit messages, docs, paper: **English only**.

## Team
| Member | Primary area |
|---|---|
| Md. Rifat Hossain | testbed (kind + observability stack), chaos engineering (Chaos Mesh), evaluation harness, deployment/packaging |
| Abdullah All Moneem | ingestion (Prometheus/Loki/Jaeger/Alertmanager collectors), detection (Z-score/EWMA/error spike), correlation (windowing, dedup, state machine) |
| Rayhan Islam Prome | RCA (graph + ranking + change correlation), LLM explainer (prompts, validator, redaction), alerting (routing, dedup, sinks), storage (Postgres + audit log) |
Everyone writes paper sections. Supervisor (Golam Moktader Nayeem) reviews before any submission.

## Architecture (5 stages)
Ingestion (Prometheus, Loki, Jaeger/OTLP, Alertmanager webhook -> Redis Streams) ->
Correlation + Detection (normalize, time-window, Z-score/EWMA, incident assembler) ->
RCA (dependency graph, evidence aggregator, change correlation, ranking) ->
Explanation + Alerting (LLM summary, dedup, severity router, payload) ->
Storage (PostgreSQL incidents + audit log).

## Locked design decisions (change only via docs/decision-tree.md change log + user approval)
- D1. **RCA is deterministic.** The LLM only summarizes evidence. It never picks the root cause.
- D2. Dependency graph is a NetworkX DiGraph with edges **caller -> callee**. Failures propagate callee -> caller, so RCA starts at the symptom service and walks **toward its callees**. Never say "upstream" without defining it.
- D3. Detection is statistical (Z-score, EWMA, percentile, error-rate spike). No deep learning training (out of scope).
- D4. Baselines freeze while an incident is open (otherwise the fault becomes "normal").
- D5. Test app: Online Boutique (or OpenTelemetry Demo). Not Sock Shop (weak tracing). Cluster: kind only.
- D6. Evaluation: >=12 distinct scenarios, **5 runs each**, mean ± std + 95% CI, a **fault-free soak run** (30-60 min) for false alarms, dev/test split for weight tuning.
- D7. Baselines: raw Alertmanager (standard kube-prometheus-stack rules, not strawman), random, highest-error-service, deepest-erroring-span, PageRank (MicroRCA-style), LLM-only.
- D8. Out of scope: cloud (AWS/GCP/Azure), VMs, Datadog/PagerDuty, ELK, auto-remediation, production scale, DL training, security faults.
- D9. Redact secrets/PII before any text goes to an LLM. Support a local-LLM fallback (Ollama).

## Repo layout
src/inframind/{common,ingestion,detection,correlation,rca,llm,alerting,storage,api}/
tests/{unit,integration,e2e}/ · evaluation/{scenarios,runner,baselines,scoring,notebooks,results}/
testbed/{kind,helm-values,chaos}/ · deploy/{docker,helm/inframind}/ · docs/ · paper/ · .claude/

## Commands
Python >=3.11, venv at `.venv`. Makefile uses `python3 -m <tool>`, so activate the venv first.
- `make help` lists every target. CI (`.github/workflows/ci.yml`) runs ruff check/format on `src tests`, `mypy src`, unit `pytest`, and a non-blocking hadolint, so `make lint` + `make test` mirror it.
- Setup: `make install` (editable + dev extras), `make precommit`.
- Gates: `make lint` (ruff check + ruff format --check + `mypy src`), `make format` (auto-fix), `make test` (pytest, config in `pyproject.toml`), `make test-all` (`-m ""`, adds integration/e2e).
- Single test: `python3 -m pytest tests/unit/common/test_bus.py::test_name -q`.
- Integration tests (`tests/integration/`, marker `integration`) need the compose Redis (`make up` or `docker compose up -d redis`) and skip when it is unreachable.
- Testbed (kind-only; context `kind-inframind`, kubeconfig written to `testbed/.kube/config`, never `~/.kube`): `make up` chains `kind -> obs -> boutique -> chaos -> docker compose up`; `make down` deletes the cluster and compose volumes; `make smoke-testbed` is the P1 gate (6 checks); per-piece `make kind|obs|boutique|chaos[-status|-delete]`. Tool and chart versions are pinned in Makefile variables; do not bump without re-running smoke.
- Ingestion (P3): run a collector with `python -m inframind.ingestion.{prometheus,loki,jaeger}` and the Alertmanager webhook with `python -m inframind.api.app` (needs `INFRAMIND_ALERT_WEBHOOK_SECRET`; port 8080 is what the in-cluster Alertmanager targets). `make smoke-ingestion` is the live P3 gate (needs `make up`); `make observe-fault [CHAOS_CR=…]` prints a before/during/after timeline around one chaos fault. Check signals with `docker exec inframind-redis redis-cli XLEN stream:signals` (the host has no `redis-cli`).
- Detection (P4): `python -m inframind.correlation.worker` runs the "Detection + Correlation" stage (`stream:signals` to `stream:incidents`). `make smoke-detection` is the live P4 gate (about 7 minutes, needs `make up`). Detector tunables: `INFRAMIND_DETECTION_<DETECTOR>_<PARAM>`; freeze lease `BASELINE_FREEZE_WINDOW`; incident cooldown `INFRAMIND_CORRELATION_COOLDOWN_S`.
- `make eval`, `make eval-all`, `make paper` are stubs until P7/P9. Docker/Helm packaging is P8.
- Host needs `fs.inotify.max_user_instances >= 512` for kind (`bootstrap.sh` fails fast otherwise). Dev box is ~7 GiB: the slim 1-control-plane/1-worker profile is the default.

## Code architecture (what spans files)
- **Stage handoff is the Redis bus, never direct calls.** Streams are pinned in `common/stream_keys.py` (`stream:signals`, `stream:incidents`, `stream:graph-updates`); renaming one is a D-change. Redis is a length-capped conduit, Postgres is the source of truth (D20). Only Alertmanager ingress is HTTP (API webhook); all other ingestion is collector pull.
- **`common/` is the shared kernel**: `Signal` (frozen pydantic, ULID id, tz-aware UTC `ts` only, lower-kebab `service`, extra fields forbidden), `bus.py`, `ids.py`, `time.py` (use `utcnow()`, never `datetime.utcnow()`), `logging.py` (JSON), `exceptions.py`, `graph_update.py` (`GraphUpdate`, the call edge on `stream:graph-updates`), `collector_state.py` (restart-safe cursors and seen-keys), `redact.py` (secret/PII redaction, D9). Signal shape: `docs/standards/03-signal-schema.md` + the code. `service_from_pod` maps pod names to services: ReplicaSet/StatefulSet suffixes are stripped, DaemonSet and static control-plane pods are kept whole.
- **Bus semantics (`common/bus.py`)**: at-least-once delivery. `publish` XADDs with approximate MAXLEN and silently drops signals whose service is `unknown`. `SignalConsumer` uses consumer groups + XAUTOCLAIM redelivery and a per-group dedup key keyed by `Signal.id`, written after handler success and before ACK. Handler errors leave the entry pending; poison messages are ACKed. Handlers must tolerate a rare double run.
- **Module dependency DAG is a hard rule** (standards 01 section 1.3): `common <- ingestion/detection <- correlation`; `rca` depends only on `common`; `llm` -> `common, rca`; `alerting` -> `common, rca, llm`; `storage` -> `common, correlation, rca`; `api` -> `common, storage, alerting` and is the only importer of FastAPI. `rca` must never import `llm/alerting/storage/api`. No new top-level repo directory without a D-number.
- **Ingestion**: three pull collectors in `ingestion/` (Prometheus per-pod series, Loki warning/error lines, Jaeger spans + `GraphUpdate`), each with a pure mapping function, `poll_once`/`run`, and `runner.run_collector` as entrypoint. A collector must never publish twice and never lose one: cursor + seen-set in `CollectorState`, key marked only after a successful publish, free text redacted before truncation. The Alertmanager webhook is `POST /webhooks/alertmanager` in `api/alertmanager.py` (only `api/` imports FastAPI); it resolves `service` from `deployment`, then `pod`, then `service`, then `job`, because the Prometheus Operator stamps `service` with the exporter's Service.
- **Detection (D3/D4)**: `DetectionEngine` runs four per-service detectors (Z-score, EWMA, error-count spike, p95 regression). Baselines freeze while an incident is open, but frozen detectors **keep scoring** (the freeze is a renewable lease), time is the signal's `ts`, a redelivered signal is a no-op, and resource gauges alert upward only. `Anomaly` and the stream `Incident` live in `common/`. P4's `IncidentTracker` is minimal and per service; cross-service grouping is P4b.
- **RCA ownership of the graph (D21/D22)**: `ServiceGraph` lives in the RCA worker; the OTel/Jaeger collector emits `GraphUpdate` on `stream:graph-updates` and never mutates the graph. Change correlation (`w4`) comes from an in-process K8s API watch with a 30-min pre-window. Details: `docs/standards/13-*.md`, `14-*.md`.
- **Testbed**: Online Boutique v0.10.7 vendored under `testbed/boutique/base` and customised only via `testbed/boutique/patches/` (kustomize). adservice, cartservice, shippingservice and emailservice emit no traces, so the service graph is partial. Chaos CRs in `testbed/chaos/` must pass server-side dry-run.
- `src/inframind/main.py` is still a scaffold; real service entrypoints land with their phase. Current phase and open work: `docs/PROGRESS.md`.

## Process gotchas
- Locked decisions go well beyond D1-D9 (up to D25; see `docs/decision-tree.md`). Read the relevant D-section before touching RCA, graph, hooks, or commit identity.
- Branches are fixed (`dev-rifat|dev-moneem|dev-prome` -> `dev` -> `main`); no topic/feature branches. Conventional Commits. D25: author `user.name` must equal the local part of a `<username>@users.noreply.github.com` email, and **no AI `Co-Authored-By:` trailers**; the D24 hook may reject extra AI attribution trailers, so check `docs/standards/08-commit-protocol.md` before adding any.
- Ruff: line length 100, rules E,F,W,I,B,UP,SIM,RUF; mypy runs on `src` only; pytest runs with `--strict-markers` and `asyncio_mode=auto`.
- pre-commit blocks commits on `main` and runs `detect-private-key`; `check-yaml` excludes vendored Boutique manifests.

## Agents (delegate; see .claude/agents/) — all on claude-opus-4-8
Organized by category under `.claude/agents/<category>/`:

- **planning/**: `project-planner`, `architect`
- **build/**: `testbed-engineer`, `ingestion-engineer`, `detection-engineer`, `correlation-engineer`, `rca-engineer`, `llm-explainer`, `alerting-storage-engineer`, `devops-packager`
- **quality/**: `test-writer`, `code-reviewer`
- **evidence/**: `evaluation-engineer`, `results-analyst`
- **writing/**: `literature-scout`, `reference-verifier`, `paper-writer`, `paper-reviewer`, `docs-writer`

## Skills (see .claude/skills/) — use them, don't reinvent
Entrypoints link to `docs/standards/` (see `docs/AGENT_WORKFLOW.md`). List: commit-protocol, phase-gates, python-standards, signal-schema, add-collector, add-detector, rca-scoring, llm-evidence-explainer, testbed-ops, fault-scenario, run-evaluation, helm-packaging, paper-writing, reference-verification, results-to-latex

## Agent workflow
Follow [`docs/AGENT_WORKFLOW.md`](docs/AGENT_WORKFLOW.md): read progress, pick one agent, load its skill, work on `dev-rifat` / `dev-moneem` / `dev-prome`, gates green, then commit. Skills point at `docs/standards/`. Phase checklists: `phase-gates` skill.

## Definition of Done (any task)
1. Code + type hints + docstrings on public functions. 2. Unit tests written and passing. 3. `make lint` clean.
4. docs/PROGRESS.md updated. 5. Committed per `commit-protocol`. 6. For anything touching RCA/eval: reproducible from a scenario id + seed.

## Hard rules
- Never commit secrets, `.env`, kubeconfigs, or datasets >5 MB. Never `--no-verify`, never force-push, never push to `main` directly.
- kubectl/helm only against `kind-*` contexts.
- Never invent a number, citation, or result. Paper numbers come only from `evaluation/results/`.
- Do not expand scope beyond D8. If a task needs it, stop and ask.
- Before big work, read `docs/PROGRESS.md` and `docs/decision-tree.md`. After work, update `docs/PROGRESS.md`.
- When unsure about a requirement, ask one focused question instead of guessing.
