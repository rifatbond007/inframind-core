# Repository layout

```
inframind/
├── pyproject.toml              # Python package + tool config (ruff, mypy, pytest)
├── Makefile                    # developer entry point (install, lint, test, up, ...)
├── Dockerfile                  # multi-stage image for InfraMind services
├── docker-compose.yml          # local dev stack: Redis + Postgres (Step 0)
├── docker-compose.override.yml.example
├── .env.example                # template for local secrets (real .env is gitignored)
├── .pre-commit-config.yaml     # local CI hooks (ruff, format, hygiene checks)
├── .editorconfig
├── .gitignore
├── LICENSE                     # MIT
├── README.md                   # top-level overview + quickstart
├── CHANGELOG.md                # per-version change log
├── CLAUDE.md                   # PROJECT MEMORY — read this first
├── PROGRESS.md             # phase checklist + session log (lives at the repo root)
│
├── src/inframind/              # the Python package (installed via `pip install -e`)
│   ├── __init__.py             # version, package docstring
│   ├── main.py                 # placeholder entrypoint
│   ├── common/                 # shared utils, signal schema, config
│   ├── ingestion/              # collectors: Prometheus, Loki, Jaeger, Alertmanager
│   ├── detection/              # Z-score, EWMA, error spike, latency regression
│   ├── correlation/            # sliding window, dedup, state machine
│   ├── rca/                    # dependency graph, ranking, evidence (CORE)
│   ├── llm/                    # prompts, validator, evidence grounding
│   ├── alerting/               # routing, dedup, payload
│   ├── storage/                # Postgres incident store + audit log
│   └── api/                    # FastAPI surface
│
├── tests/                      # test code (mirrors src layout)
│   ├── unit/                   # fast, no external deps (CI runs these)
│   ├── integration/            # needs Redis/Postgres
│   └── e2e/                    # needs the K8s testbed
│
├── evaluation/                 # Phase 7: fault scenarios, runner, baselines, scoring
│   ├── scenarios/              # YAML per scenario (fault type, target, duration, GT)
│   ├── runner/                 # script that applies Chaos Mesh CRs and records output
│   ├── baselines/              # random, highest-error, deepest-error, PageRank, LLM-only
│   ├── scoring/                # precision, recall, MTTD, MTTR, false alarms/hour
│   ├── notebooks/              # Jupyter analysis (kept small; results go in results/)
│   └── results/                # generated CSVs / figures (gitignored except .gitkeep)
│
├── testbed/                    # Phase 1: kind cluster, Helm values, Chaos Mesh
│   ├── kind/                   # kind config + bootstrap script
│   ├── helm-values/            # kube-prometheus-stack, Loki, Jaeger, Online Boutique
│   └── chaos/                  # Chaos Mesh CRs
│
├── deploy/                     # Phase 8: how to package InfraMind itself
│   ├── docker/                 # per-service Dockerfiles
│   └── helm/inframind/         # Helm chart for the InfraMind stack
│
├── docs/                       # human-readable documentation
│   ├── ARCHITECTURE.md         # five-stage pipeline diagram
│   ├── REPO_LAYOUT.md          # this file
│   ├── CONTRIBUTING.md         # branch / commit / PR rules
│   ├── DECISIONS.md            # decision log (ADR-lite, the deltas)
│   ├── decision-tree.md        # locked decisions (WHAT and WHY) — the source of truth
│   ├── surface-map.md          # signals-in / incidents-out / env keys / upstream contract
│   ├── standards/              # engineering conventions handbook (12 numbered files + README)
│   │   ├── README.md           #   precedence, scope, glossary, every-rule-on-one-screen
│   │   ├── 01-project-structure.md
│   │   ├── 02-python-standards.md
│   │   ├── 03-signal-schema.md
│   │   ├── 04-add-collector.md
│   │   ├── 05-add-detector.md
│   │   ├── 06-rca-scoring.md
│   │   ├── 07-llm-explainer.md
│   │   ├── 08-commit-protocol.md
│   │   ├── 09-testbed-ops.md
│   │   ├── 10-evaluation.md
│   │   ├── 11-helm-packaging.md
│   │   └── 12-paper-writing.md
│   └── proposal.pdf            # original BSc capstone proposal (reference)
│
├── paper/                      # Phase 9: LaTeX source, figures, bibliography
│
└── .github/                    # GitHub-side config
    ├── workflows/ci.yml        # lint + test on every push and PR
    ├── CODEOWNERS              # auto-request review per directory
    ├── PULL_REQUEST_TEMPLATE.md
    └── dependabot.yml
```

## What each top-level directory is for

- **`src/inframind/`** — the only place functional code lives. Installed via `pip install -e .`.
- **`tests/`** — mirrors `src/inframind/`. CI runs `tests/unit/`. Integration and e2e are opt-in locally.
- **`evaluation/`** — built in Phase 7. Drives Chaos Mesh, scores InfraMind's output.
- **`testbed/`** — built in Phase 1. Provisions the kind cluster and observability stack.
- **`deploy/`** — built in Phase 8. Helm chart for InfraMind itself.
- **`docs/`** — internal documentation. The proposal PDF is here for reference only.
- **`paper/`** — built in Phase 9. LaTeX, figures, references.

## What is intentionally not in this repo

- Real secrets / `.env` / kubeconfigs / large datasets.
- Generated artifacts under `evaluation/results/` (committed only as `.gitkeep`).
- Build output from `paper/` (LaTeX aux/bbl/etc).
