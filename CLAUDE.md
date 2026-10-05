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

## Locked design decisions (change only via docs/DECISIONS.md + user approval)
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

## Commands (created progressively; if missing, create them via the devops-packager agent)
`make up` / `make down` · `make test` · `make lint` · `make eval SCENARIO=<id>` · `make eval-all` · `make paper`

## Agents (delegate; see .claude/agents/) — all on opus 4.8
Organized by category under `.claude/agents/<category>/`:

- **planning/**: `project-planner`, `architect`
- **build/**: `testbed-engineer`, `ingestion-engineer`, `detection-engineer`, `correlation-engineer`, `rca-engineer`, `llm-explainer`, `alerting-storage-engineer`, `devops-packager`
- **quality/**: `test-writer`, `code-reviewer`
- **evidence/**: `evaluation-engineer`, `results-analyst`
- **writing/**: `literature-scout`, `reference-verifier`, `paper-writer`, `paper-reviewer`, `docs-writer`

## Skills (see .claude/skills/) — use them, don't reinvent
commit-protocol, phase-gates, python-standards, signal-schema, add-collector, add-detector, rca-scoring, llm-evidence-explainer, testbed-ops, fault-scenario, run-evaluation, helm-packaging, paper-writing, reference-verification, results-to-latex

## Slash commands
/status · /start-phase · /commit · /review · /new-scenario · /run-eval · /verify-refs · /draft-section

## Definition of Done (any task)
1. Code + type hints + docstrings on public functions. 2. Unit tests written and passing. 3. `make lint` clean.
4. docs/PROGRESS.md updated. 5. Committed per `commit-protocol`. 6. For anything touching RCA/eval: reproducible from a scenario id + seed.

## Hard rules
- Never commit secrets, `.env`, kubeconfigs, or datasets >5 MB. Never `--no-verify`, never force-push, never push to `main` directly.
- kubectl/helm only against `kind-*` contexts.
- Never invent a number, citation, or result. Paper numbers come only from `evaluation/results/`.
- Do not expand scope beyond D8. If a task needs it, stop and ask.
- Before big work, read docs/PROGRESS.md and docs/DECISIONS.md. After work, update PROGRESS.md.
- When unsure about a requirement, ask one focused question instead of guessing.
