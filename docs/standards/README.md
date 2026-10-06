# standards — InfraMind engineering conventions

**Split-by-convention layout.** One numbered file per convention area (`NN-*.md`), so each
area is independently reviewed, blamed and updated.

**Stack:** **Python 3.11+** · FastAPI · Pydantic v2 · NetworkX · Redis Streams · PostgreSQL 16 ·
kind · Helm · kube-prometheus-stack · Loki · Jaeger / OTel · Chaos Mesh. **Server-first**: the
service runs as a set of long-lived workers in K8s, not as a web app in a browser.

**Read [`CLAUDE.md`](../../CLAUDE.md) first.** It carries the project memory, the locked design
decisions (D1–D9), the team table, and the agent + skill index. This set *expands* it; it never
contradicts it.

**Pair with [`../decision-tree.md`](../decision-tree.md)**: that file says **WHAT** the project is
and **WHY**; this set says **HOW** the code is written. Where they meet, the decision tree wins —
this set stays portable.

**Anything true of one agent only lives in that agent's `AGENTS.md`** (under
`.claude/agents/<category>/<name>.md) — this code, this scope, this output format. This set stays
portable; the agent files override where they meet.

**Cross-references use the word "section".** Write `section 6.2` — never the typographic section
symbol. Applies to every doc in `docs/`.

**This set is a snapshot.** When a procedural detail changes in
`.claude/skills/<name>/SKILL.md`, update the corresponding standards file in the same PR.

---

## 0. Every rule on one screen

| # | Area | Rule |
|---|---|---|
| 1 | [Project structure](01-project-structure.md) | `src/inframind/` is the only place functional code lands. Each subpackage is one pipeline stage with one owner. Inter-module dependency rules are enforced by convention and by the lint config. Hard rules are honoured at every layer. |
| 2 | [Python standards](02-python-standards.md) | Python 3.11+. Type hints on every public function. Google-style docstrings. `from __future__ import annotations`. Modern union syntax. `ruff format` + `ruff check`. Line length 100. |
| 3 | [Signal schema](03-signal-schema.md) | `Signal` is the canonical representation. `ts` is observation time, not publish time. `service` is normalised lower-kebab. `attrs` is JSON-serializable, no secrets (D9). Every collector maps its backend payload to `Signal` before crossing a module boundary. |
| 4 | [Add a collector](04-add-collector.md) | `async def run()` plus `_to_signal()` plus `_publish()`. Idempotent publish. Optional logging for retries and failures. Smoke check via `redis-cli XLEN`. |
| 5 | [Add a detector](05-add-detector.md) | `update()` per-signal stateful, `freeze()` / `unfreeze()` for incident lifecycle (D4). State is per-service. Tune thresholds on dev split, report on test split. |
| 6 | [RCA scoring](06-rca-scoring.md) | Deterministic ranking. Edges `caller -> callee`. Walk from symptom toward callees (D2). Same `(graph, anomaly_set)` produces the same score — regardless of LLM version. |
| 7 | [LLM explainer](07-llm-explainer.md) | LLM summarises evidence; the graph decides (D1). Validator rejects claims citing missing evidence IDs. Redact secrets/PII before any text leaves the process (D9). Ollama is the local fallback. `temperature=0`. |
| 8 | [Commit protocol](08-commit-protocol.md) | Conventional Commits. Branches: `main`, `dev`, `dev-rifat`, `dev-moneem`, `dev-prome` only. Linear history, squash-merge. Direct commits to `main` blocked by pre-commit hook. |
| 9 | [Testbed ops](09-testbed-ops.md) | `make up` brings up the stack in order; `make down` tears it down. `kubectl`/`helm` only against `kind-*` contexts. Never against the default context. Smoke check before trusting the testbed. |
| 10 | [Evaluation](10-evaluation.md) | `make eval SCENARIO=<id>`. ≥12 scenarios x 5 reps + fault-free soak (D6). 6 baselines (D7). Dev/test split — never tune on test. Every scenario has `seed`. |
| 11 | [Helm packaging](11-helm-packaging.md) | `deploy/helm/inframind/`. No real secrets in `values.yaml`. Read-only RBAC. `helm lint` before commit. |
| 12 | [Paper writing](12-paper-writing.md) | IEEE conference, 6–8 pages, LaTeX. Never invent a number (cite the CSV). Never invent a citation (use `reference-verifier`). Honour D1, D2, D7 in the wording. |

**Structure** 01  **Code craft** 02 · 03 · 04 · 05 · 06 · 07  **Process** 08 · 09 · 10 · 11  **Paper** 12

---

## Precedence (highest first)

1. **Running code** — the deployed behaviour wins when there is a conflict.
2. **[`../decision-tree.md`](../decision-tree.md)** — locked decisions; the source of WHAT and WHY.
3. **[`CLAUDE.md`](../../CLAUDE.md)** — hard rules, team table, agent index.
4. **This set** — how the code is written.
5. **[`.claude/skills/`](../../.claude/skills/)** — agent entrypoints; procedural detail lives in this standards set.
6. **`.claude/agents/<category>/<name>.md`** — per-agent scope and output style.

A skill that disagrees with a file here loses. A file here that disagrees with `decision-tree.md`
loses. `decision-tree.md` that disagrees with running code loses.

---

## Scope

| Surface | What lives there |
|---|---|
| `docs/standards/` | This set. Human-readable narrative of the conventions. |
| `.claude/skills/<name>/SKILL.md` | Procedural how-to. Read by an agent before doing a specific job. |
| `docs/decision-tree.md` | Locked decisions, with WHY. The source of truth for the project's shape. |
| `docs/surface-map.md` | The wire shapes: signals in, incidents out, env keys, upstream contract. |
| `docs/ARCHITECTURE.md` | Five-stage pipeline diagram. |
| `CLAUDE.md` | Project memory — read first. |

When in doubt, **read the standards file for the area**. If it points at a skill, follow the skill
for the step-by-step.

---

## Glossary

Universal terms. Domain vocabulary stays in the relevant section.

| Term | Meaning |
|---|---|
| **Signal** | Pydantic model in `inframind.common.signal`. The canonical representation of every observation that enters the pipeline. |
| **Incident** | A grouped set of related anomalies and evidence, with a ranked candidate list. |
| **Candidate** | One possible root cause service and its evidence bundle. |
| **Evidence** | A single item in a candidate's bundle (metric snapshot, log line, trace span, change event). |
| **The bus** | Redis Streams. The conduit between stages — never a source of truth. |
| **The store** | PostgreSQL. Source of truth for incidents, evidence, alerts, audit log. |
| **Phase** | One of P0–P9 in `CLAUDE.md`. |
| **Phase gate** | The checklist that must pass before moving from one phase to the next — see `phase-gates` skill. |
| **Dev/test split** | The dev split is for tuning. The test split is for the final report. Never tune on test. |
| **Scenario** | A YAML under `evaluation/scenarios/<id>/scenario.yaml`. Reproducible from `scenario_id + seed`. |
| **Baseline** | A ranker alternative compared against InfraMind's ranker in the paper (D7). |
| **Reproduction seed** | The `seed` field on every scenario. Required. |
| **Hard rule** | A rule that cannot be overridden without supervisor sign-off and a new entry in `docs/decision-tree.md` (change log). |
| **Soft rule** | A convention. May be deviated from with a comment explaining why. |

---

## How to use this set

1. **Onboarding** — read the README (this file), then `01-project-structure`, then
   `02-python-standards`. After that, jump by file number.
2. **Before a new module** — read `01-project-structure` to confirm where it lives and who owns it.
   Read `02-python-standards` for the Python rules. Read the file for the closest functional area
   (collector → 04, detector → 05, RCA → 06, LLM → 07).
3. **Before a commit** — read `08-commit-protocol`. No exceptions.
4. **Before deploying / running the testbed** — read `09-testbed-ops` and `11-helm-packaging`.
5. **Before evaluating** — read `10-evaluation`.
6. **Before writing the paper** — read `12-paper-writing`.

---

## How to update this set

- The **prose** lives here. The **steps** live in the matching skill.
- If you change a convention here, double-check the corresponding skill still agrees. If you change
  a skill, update the standard in the same PR.
- If a new convention appears that the standards do not cover, add a new numbered file. **Never**
  silently edit an existing file to swallow the new rule.
- New files get the next number. Re-numbering is not done unless the team agrees.
