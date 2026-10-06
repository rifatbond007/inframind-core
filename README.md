# InfraMind

Kubernetes multi-signal incident detection and graph-guided root cause analysis.

> BSc capstone, BAIUST CSE. Supervisor: Golam Moktader Nayeem.

When a fault hits a Kubernetes microservice app, InfraMind says **which service is the root cause, with evidence**, and sends one deduplicated alert.

## Status

**Phase P0 — Repo & tooling setup.** No functional code yet. See `docs/PROGRESS.md` for the phase plan and `CLAUDE.md` for the locked design decisions.

## Quickstart

```bash
# 1. Install (editable, with dev extras)
make install

# 2. Start the local dev stack (Redis + Postgres via docker compose)
make up

# 3. Run lint + tests
make lint
make test
```

## Agentic workflow

InfraMind is shipped by an **in-repo AI team**: 19 agents grouped into 5 categories, each
with one of 3 human owners (Rifat, Moneem, Prome — see `CLAUDE.md`). Work flows
left-to-right through the categories; every phase has a phase-gate
(`.claude/skills/phase-gates`). All agents run on `opus 4.8` and live under
`.claude/agents/<category>/<name>.md`.

> *Every phase of the work has an owner. The loop below is the project's execution
> flow. The arrows show the dominant path; any category can call any other when the scope
> requires it.*

```
                planning             build              quality
              ┌──────────┐        ┌──────────┐        ┌──────────┐
              │ 2 agents │ ─────► │  8 agents│ ─────► │ 2 agents │
              └──────────┘        └──────────┘        └──────────┘
                    │                                       │
                    │                                      ▼
                    │                                ┌──────────┐
                    │                                │ evidence │
                    │                                │ 2 agents │
                    │                                └──────────┘
                    │                                      │
                    │                                      ▼
                    │              ┌──────────┐
                    └────────────► │  writing │
                                   │ 5 agents │
                                   └──────────┘
                                         │
                                         ▼
                                   commit-protocol
                                         │
                                         ▼
                                   docs/PROGRESS.md
```

### Who owns which category

| Category | Owns | Owned by |
|---|---|---|
| `planning/` | Phase sequencing, architecture | team |
| `build/` | Code under `src/inframind/<area>/` | Rifat (testbed, packaging), Moneem (ingestion, detection, correlation), Prome (rca, llm, alerting, storage) |
| `quality/` | Tests, code review | team |
| `evidence/` | Evaluation harness, results analysis | Rifat (harness), Prome (analysis) |
| `writing/` | Paper + docs | all |

### Agent index (19)

**planning/** — phase sequencing and architecture

| Agent | Role | Phase |
|---|---|---|
| `project-planner` | Owns the phase plan, scheduling, and dependency map. Reads `CLAUDE.md`, `docs/PROGRESS.md`, `docs/decision-tree.md`. | every |
| `architect` | Owns the five-stage pipeline design, module boundaries, and interface contracts between stages. | every |

**build/** — code under `src/inframind/<area>/`

| Agent | Role | Phase |
|---|---|---|
| `testbed-engineer` | Owns the kind cluster, observability stack, Chaos Mesh, and the sample microservice application. | P1 |
| `ingestion-engineer` | Owns the collectors that pull Prometheus, Loki, Jaeger/OTLP, and Alertmanager into the unified `Signal` stream. | P3 |
| `detection-engineer` | Owns statistical anomaly detectors (Z-score, EWMA, log error-rate spike, trace p95 latency regression). | P4 |
| `correlation-engineer` | Owns sliding-window grouping, fingerprint dedup, and the incident state machine. | P4b |
| `rca-engineer` | Owns the dependency graph, traversal, ranking, and evidence bundling — the CORE contribution. | P5 |
| `llm-explainer` | Owns the LLM explainer: prompts, validator, evidence grounding, redaction, and the local Ollama fallback. | P5b |
| `alerting-storage-engineer` | Owns alert routing, deduplication, payload builder, and the PostgreSQL incident store + audit log. | P6 |
| `devops-packager` | Dockerfiles, the Helm chart for InfraMind itself, and `make up` for the full stack. | P8 |

**quality/** — tests and review

| Agent | Role | Phase |
|---|---|---|
| `test-writer` | Owns unit, integration, and end-to-end tests. Every functional change ships with reproducible tests. | every |
| `code-reviewer` | Reviews PRs for correctness, style, reproducibility, and adherence to D1–D9. | every PR |

**evidence/** — evaluation and results

| Agent | Role | Phase |
|---|---|---|
| `evaluation-engineer` | Owns fault-injection scenarios, the evaluation runner, the baselines, and the scoring harness. | P7 |
| `results-analyst` | Reads `evaluation/results/` and turns them into tables, figures, and prose claims for the paper. Never invents numbers. | P7 → P9 |

**writing/** — paper and docs

| Agent | Role | Phase |
|---|---|---|
| `literature-scout` | Surveys published work on observability, anomaly detection, RCA, and LLM-assisted diagnosis. Maintains references and related-work sections. | P9 |
| `paper-writer` | Drafts the research paper in IEEE conference LaTeX. | P9 |
| `paper-reviewer` | Reviews the draft before submission — number provenance, citation accuracy, novelty claim, D1–D9 compliance. | P9 |
| `docs-writer` | Maintains in-repo documentation (README, ARCHITECTURE, REPO_LAYOUT, CONTRIBUTING, PROGRESS, decision-tree). | every |
| `reference-verifier` | Verifies every reference in `paper/references.bib` and `proposal.pdf`. Removes anything that cannot be confirmed. | P9 |

Full per-agent contract (scope, hard rules, output style) lives in
`.claude/agents/<category>/<name>.md`. Pair with `.claude/skills/` for task entrypoints (each skill points at `docs/standards/`) —
e.g. `add-collector`, `phase-gates`, `commit-protocol`, `run-evaluation`,
`paper-writing`, `reference-verification`.

## Documentation

- `CLAUDE.md` — project memory, locked design decisions, repo layout, agent & skill index. Read this first.
- `docs/PROGRESS.md` — phase checklist and session-by-session log.
- `docs/decision-tree.md` — locked decisions (WHAT and WHY), change log (ADR-lite), and proposal deltas — single source of truth.
- `docs/surface-map.md` — signals-in / incidents-out / env keys / upstream contract.
- `docs/AGENT_WORKFLOW.md` — how agents, skills, and standards relate (read before delegating work).
- `docs/standards/` — engineering conventions handbook. 12 numbered files (project structure, Python, signal schema, collectors, detectors, RCA, LLM, commits, testbed, evaluation, Helm, paper) + a README index. Pairs with `.claude/skills/`.
- `docs/ARCHITECTURE.md` — five-stage pipeline overview.
- `docs/REPO_LAYOUT.md` — what each directory is for.
- `docs/CONTRIBUTING.md` — branching, commits, PR workflow.
- `docs/proposal.pdf` — original BSc capstone proposal (reference only).

## License

MIT — see `LICENSE`.
