# InfraMind Progress Log
Update after every work session (newest first). Agents: read this before starting.

## Current phase
Phase 0 — Repo + tooling setup (Step 0 complete locally; awaiting repo creation + branch protection on GitHub)

## Phase checklist
- [x] P0 Repo/tooling
- [ ] P1 Testbed (kind + Online Boutique + observability stack + Chaos Mesh)
- [ ] P2 Schema + event bus (Pydantic `Signal` + Redis Streams)
- [ ] P3 Ingestion collectors (Prometheus/Loki/Jaeger/Alertmanager)
- [ ] P4 Detection (Z-score, EWMA, error spike, latency regression)
- [ ] P4b Correlation (windowing, dedup, state machine)
- [ ] P5 RCA (graph + ranking + PageRank variant)
- [ ] P5b LLM explainer (prompts, validator, redaction, Ollama fallback)
- [ ] P6 Alerting + Storage + API
- [ ] P7 Evaluation harness (≥12 scenarios × 5 reps + soak + baselines + ablations)
- [ ] P8 Packaging / deployment (Dockerfiles + Helm chart + `make up` for everything)
- [ ] P9 Paper (3–4 weeks)

## Log
| Date | Who | Done | Next | Blockers |
|---|---|---|---|---|
| 2026-10-05 | team | .claude folder created | run setup.sh, init repo | none |
| 2026-10-05 | team | **Step 0 done**: pyproject.toml, Makefile, ruff/mypy/pytest config, .gitignore, .editorconfig, pre-commit, .env.example, LICENSE, README, CHANGELOG, Dockerfile, module skeletons (`common/ingestion/detection/correlation/rca/llm/alerting/storage/api`), `main.py`, docker-compose (Redis + Postgres), CI workflow (`lint` + `test` + Dockerfile lint), CODEOWNERS, PR template, dependabot, `docs/ARCHITECTURE.md`, `docs/REPO_LAYOUT.md`, `docs/CONTRIBUTING.md`, team roles filled in CLAUDE.md, D-Setup entry in DECISIONS.md. Lint + smoke tests verified. | Create the GitHub repo, configure branch protection on `main` (PR-only, no force-push, lint + test must pass, linear history), wire CODEOWNERS to real handles. Then start Step 1 (testbed). | Supervisor approval still pending on the 6 proposed deviations from the original proposal (D2/D5/D6/D7/NFR/Timeline) — not blocking but flag in supervisor sync. |
| 2026-10-05 | team | **Agents + skills scaffolded**. 19 agents (`.claude/agents/`) and 15 skills (`.claude/skills/`) committed. All agents run on `opus 4.8`; all frontmatter is valid YAML. CLAUDE.md agent list updated (added `correlation-engineer`). | Wait for supervisor approval on the 6 deviations (D2/D5/D6/D7/NFR/Timeline) before starting Step 1. | None. |
| 2026-10-05 | team | **Docs aligned.** Removed foreign `docs/frontline/`, `docs/frontline-overrides.md`, `docs/surface-map.md` (a Next.js / CashlessAI / pnpm set, 100% misaligned with InfraMind). Authored InfraMind-flavoured `docs/decision-tree.md` (locked decisions D1/D3/D4/D5/D6/D7/D9/D12/D15/D20/D-Setup) and `docs/surface-map.md` (signals in, incidents out, env keys, upstream contract, storage schema, API surface). Updated `docs/REPO_LAYOUT.md` to drop the foreign entries and add the new ones. No code change. | None — purely docs. | None. |
