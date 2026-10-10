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
| 2026-10-11 | rifatbond007 + agent | **Three new standards locked (D21–D24).** Added `docs/standards/13-service-graph-contract.md` (D21: `ServiceGraph` primitive in the RCA worker, `stream:graph-updates` bus, EWMA weights, cold-start from `RCA_GRAPH_PATH`), `docs/standards/14-change-correlation.md` (D22: K8s API watch on 6 resource types, 30-min pre-window, append-only `change_events` table, secrets metadata only), and `docs/standards/15-enforcement-hooks.md` (D23-Scripts: new `scripts/` top-level dir for dev-time tooling; D24-Hooks: three pre-commit hooks — DAG, trailer, PROGRESS.md — rolled out in `--simulate` for one week before enforcing). Updated `docs/decision-tree.md` with the four D-sections and four change-log rows (status: approved by team, supervisor sign-off pending next sync). Updated `docs/standards/README.md` index + glossary. P1 (testbed) is now unblocked on the graph contract. | P1 work on `dev-rifat` with `testbed-engineer`. Wire `.pre-commit-config.yaml` to the three hook scripts (follow-up commit; standards file 15 is the authority). Land supervisor sign-off in the next sync. | Hook script Python is not yet written — `scripts/` directory named in D23 but not created. |
| 2026-10-10 | rifatbond007 + agent | **Author identity pinned (D25).** Tightened `docs/standards/08-commit-protocol.md` section 8.2.1 to require `git config user.name` to be the GitHub username (`rifatbond007`, `moneem-07`, `promerayhan`) and `user.email` to be the matching `noreply` email. Display names in the `name` field and `Co-Authored-By:` trailers for AI tooling (Claude Code, Puku-CLI, Cursor, GitHub Copilot, Codex, JetBrains AI) are now explicitly rejected. Added D25 section to `docs/decision-tree.md` and the matching change-log row. Added a D25 checklist bullet to `.claude/agents/quality/code-reviewer.md` so reviewers verify the latest commit's author + email + trailers on every PR. Local `git config` on this repo is now `rifatbond007 <rifatbond007@users.noreply.github.com>`. | Land the actual `check_commit_message.py` allow-list logic against D25 in the D24 follow-up PR (currently the allow-list is documented in `08-commit-protocol.md` section 8.2.1 but the hook script is not yet written). | The previous commit (c61a27e, on `origin/main`) authored the D21–D24 work as `"Md. Rifat Hossain"` instead of `"rifatbond007"` and cannot be amended (pushed history, §8.9). It must be left as-is; the rule only takes effect from this commit forward. |
| 2026-10-07 | Nahid + agent | **Agent workflow.** Rebuilt `docs/AGENT_WORKFLOW.md` as the session loop (agent, skill, owner branch, gates, PR path). Root `README.md` left as the previous agent-index version. | Start P1 on `dev-rifat` with `testbed-engineer`. | None. |
| 2026-10-07 | Nahid + agent | **Single decision doc.** Merged `docs/DECISIONS.md` into `docs/decision-tree.md` (change log + D2 section); removed duplicate file; updated refs across repo. | — | None. |
| 2026-10-07 | Nahid + agent | **ARCHITECTURE.md refresh.** Rebuilt `docs/ARCHITECTURE.md` with professional Mermaid: C4 context/container, data-flow, pipeline, sequence (with Redis), layers, K8s deployment, RCA topology + walk, ER diagram. | Preview diagrams in GitHub/Cursor; continue P1 testbed. | None. |
| 2026-10-07 | Nahid + agent | **Repo structure audit vs proposal.** Moved progress log to `docs/PROGRESS.md` (single canonical path). Removed GitHub Dependabot config. Added phase skeleton dirs (`testbed/`, `evaluation/`, `deploy/`, `paper/`, `tests/integration/`, `tests/e2e/`). Slimmed `.claude/skills/` to entrypoints pointing at `docs/standards/` (except `phase-gates`). Fixed ARCHITECTURE layer-rule numbering and deployment diagram (bus-only worker handoff). Makefile default `PY=python3`. | GitHub repo + branch protection; supervisor sign-off on D2/D5/D6/D7/NFR/Timeline; start P1 testbed. | None. |
| 2026-10-05 | team | .claude folder created | run setup.sh, init repo | none |
| 2026-10-05 | team | **Step 0 done**: pyproject.toml, Makefile, ruff/mypy/pytest config, .gitignore, .editorconfig, pre-commit, .env.example, LICENSE, README, CHANGELOG, Dockerfile, module skeletons (`common/ingestion/detection/correlation/rca/llm/alerting/storage/api`), `main.py`, docker-compose (Redis + Postgres), CI workflow (`lint` + `test` + Dockerfile lint), CODEOWNERS, PR template, `docs/ARCHITECTURE.md`, `docs/REPO_LAYOUT.md`, `docs/CONTRIBUTING.md`, team roles filled in CLAUDE.md, D-Setup entry in DECISIONS.md. Lint + smoke tests verified. | Create the GitHub repo, configure branch protection on `main` (PR-only, no force-push, lint + test must pass, linear history), wire CODEOWNERS to real handles. Then start Step 1 (testbed). | Supervisor approval still pending on the 6 proposed deviations from the original proposal (D2/D5/D6/D7/NFR/Timeline) — not blocking but flag in supervisor sync. |
| 2026-10-05 | team | **Agents + skills scaffolded**. 19 agents (`.claude/agents/`) and 15 skills (`.claude/skills/`) committed. All agents run on `opus 4.8`; all frontmatter is valid YAML. CLAUDE.md agent list updated (added `correlation-engineer`). | Wait for supervisor approval on the 6 deviations (D2/D5/D6/D7/NFR/Timeline) before starting Step 1. | None. |
| 2026-10-05 | team | **Docs aligned.** Removed foreign `docs/frontline/`, `docs/frontline-overrides.md`, misaligned surface-map. Authored InfraMind-flavoured `docs/decision-tree.md` and `docs/surface-map.md`. Updated `docs/REPO_LAYOUT.md`. | None — purely docs. | None. |
