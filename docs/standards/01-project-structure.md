# 1. Project structure — repo layout, modules, ownership
> standards · Python conventions · InfraMind. Pairs with [`CLAUDE.md`](../../CLAUDE.md) (rules) and [`../decision-tree.md`](../decision-tree.md) (why). This file says where the code lives.

## 1.1 The shape

```
inframind/
├── src/inframind/             # the only place functional code lives
│   ├── common/                # Signal schema, config, logging, stream keys
│   ├── ingestion/             # collectors: Prometheus, Loki, Jaeger/OTLP, Alertmanager
│   ├── detection/             # Z-score, EWMA, error-rate spike, latency regression
│   ├── correlation/           # sliding window, dedup, incident state machine
│   ├── rca/                   # graph, traversal, ranker, evidence bundler  (CORE)
│   ├── llm/                   # prompts, validator, redaction, Ollama fallback
│   ├── alerting/              # routing, dedup, payload
│   ├── storage/               # Postgres incident store + audit log
│   └── api/                   # FastAPI surface — webhooks + /incidents
│
├── tests/                     # mirrors src layout
│   ├── unit/                   # fast, no external deps (CI runs these)
│   ├── integration/           # needs Redis + Postgres
│   └── e2e/                   # needs the K8s testbed
│
├── evaluation/                # fault scenarios, runner, baselines, scoring
├── testbed/                   # kind + Helm + Chaos Mesh + Online Boutique
├── deploy/                    # Dockerfiles + Helm chart for InfraMind itself
├── docs/                      # human-readable docs (this set lives under docs/standards/)
├── paper/                     # LaTeX source + bibliography
└── .claude/                   # 19 agents + 15 skills (the AI team)
```

## 1.2 The rule: one stage, one directory, one owner

```
+---------------------+----------------+-------------------------------------+
| Subpackage          | Pipeline stage | Owner (per CLAUDE.md)               |
+---------------------+----------------+-------------------------------------+
| common/             | Source signal / shared substrate | team       |
| ingestion/          | Stage 1 — collect signals | Moneem                     |
| detection/          | Stage 2a — statistical anomaly detection | Moneem       |
| correlation/        | Stage 2b — group anomalies into incidents | Moneem     |
| rca/                | Stage 3 — graph + ranking + evidence | Prome              |
| llm/                | Stage 4a — summarise evidence | Prome                     |
| alerting/           | Stage 4b — dedup + route + send | Prome                   |
| storage/            | Stage 5 — persist + audit | Prome                         |
| api/                | API gateway — webhooks + read API | Prome                |
+---------------------+----------------+-------------------------------------+
```

**The rule.** A new subpackage is created only when a new pipeline stage appears. A new file in
an existing subpackage is owned by the stage's owner.

## 1.3 Inter-module dependency rules

These are enforced by code review; the lint config catches the obvious cases.

- **`common/` imports nothing from InfraMind.** It depends only on third-party libraries.
- **`ingestion/` depends on `common/`.** It must not depend on `detection/` or anything downstream.
- **`detection/` depends on `common/`.** It must not depend on `correlation/` or `rca/`.
- **`correlation/` depends on `common/`, `detection/`.** It must not depend on `rca/`.
- **`rca/` depends on `common/`.** It must not depend on `llm/`, `alerting/`, `storage/`, `api/`.
- **`llm/` depends on `common/`, `rca/`.** It must not depend on `alerting/`, `storage/`, `api/`.
- **`alerting/` depends on `common/`, `rca/`, `llm/`.** It must not depend on `storage/`, `api/`.
- **`storage/` depends on `common/`, `correlation/`, `rca/`.** It must not depend on `alerting/`, `api/`.
- **`api/` depends on `common/`, `storage/`, `alerting/`.** It is the only module that imports
  FastAPI.

The dependency graph is a DAG. There are no cycles. A reviewer must reject any PR that introduces
one.

## 1.4 What lives in `common/`

Only the things that every other stage genuinely needs:

| Module | Purpose |
|---|---|
| `signal.py` | The `Signal` Pydantic model (section 3). |
| `config.py` | Pydantic-settings class reading the env keys (section 9 + `surface-map.md`). |
| `logging.py` | The structured JSON logger. Used by every module. |
| `stream_keys.py` | The pinned Redis stream key names. Single source for `stream:signals`, `stream:incidents`. |
| `exceptions.py` | The domain exception hierarchy (`IngestionError`, `DetectionError`, `RcaError`, ...). |
| `time.py` | The `utcnow()` helper. Every collector and detector uses it; never `datetime.utcnow()` directly. |
| `ids.py` | The ULID generator. Every `Signal.id`, `Incident.id`, `Candidate.id`. |

If you find yourself reaching for a third-party utility, ask: would this be useful to every
stage? If yes, it belongs here. If no, it belongs in the stage that needs it.

## 1.5 The agent team vs the code

The `.claude/` tree holds **19 agents** that ship the project. They are not part of the runtime —
the deployed code is deterministic. Hard rule:

- **No agent runtime in the deployed code.** The agents are dev-time collaborators. They do not
  appear in the production image. The deployment footprint is `src/inframind/` plus its
  dependencies.
- **No "agent router" inside `src/inframind/api/`.** The API is a FastAPI app with deterministic
  handlers, not an LLM-driven controller.

The agents produce the code; the code does the work.

## 1.6 Hard rules

- **No new top-level directory without a D-number.** Anything that wants `benchmarks/`, `data/`,
  `models/`, `notebooks/` at the repo root needs a new entry in `docs/decision-tree.md` (change log) and supervisor
  sign-off. `evaluation/notebooks/` exists as an exception (locked in P7).
- **The dependency DAG is a hard rule.** If you need an upstream dependency that creates a cycle,
  fix the design — do not relax the rule.
- **No secrets in the repo.** `.env.example` carries placeholder values. The real `.env` is
  gitignored. `.gitignore` blocks `kubeconfig*`, `*.kubeconfig`, `*.pem`, `*.key`.
- **No DL training in code.** D3. Statistical detectors only.

## 1.7 What never goes in code

| Forbidden | Why | Use instead |
|---|---|---|
| `print()` for logging | Breaks structured logging. | `inframind.common.logging.get_logger(__name__)`. |
| `assert` for input validation | Stripped under `-O`. | Pydantic validators or explicit `if ... raise`. |
| Bare `except:` | Catches `KeyboardInterrupt` and `SystemExit`. | Catch a specific exception; log; re-raise or handle. |
| `except Exception` without re-raising | Swallows bugs. | Catch, log, re-raise, or convert to a domain exception. |
| `# noqa` without a reason | Hides lint failures. | Fix the lint failure, or add a comment explaining the exception. |
| Magic numbers without a name | Untraceable. | A module-level constant in `common/config.py` or a per-module constants block. |

## 1.8 Cross-reference

- **Locked decisions:** `docs/decision-tree.md` — D-Setup, D2, D3, D8, D20.
- **Skill:** none — this is a structural doc, not a procedure.
- **Per-agent ownership:** `CLAUDE.md` — Team table (the source of truth for who owns what).
- **Repo layout reference:** `docs/REPO_LAYOUT.md` — directory tree with one-line purpose.
- **Pipeline shape:** `docs/ARCHITECTURE.md` — five-stage diagram.
