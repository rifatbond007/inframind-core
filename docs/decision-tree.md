# Decision tree — InfraMind

> **Source of truth for WHAT InfraMind is and WHY.** Pairs with [`ARCHITECTURE.md`](ARCHITECTURE.md)
> (the diagram) and [`REPO_LAYOUT.md`](REPO_LAYOUT.md) (where the code lives). This file decides
> things; the others describe the consequences.
>
> Every decision below is **locked** — to change one, append a row to
> [Change log](#change-log-adr-lite) (date, change, trigger, supervisor sign-off when required).
> The hard rules in `CLAUDE.md` (no DL training, no scope creep, no
> invented numbers) are not repeated here; they cannot be overridden by any decision below.
>
> Decisions are numbered `D-NN` or `D-<name>`. Older IDs come first. Proposal deltas stay in the
> change log until supervisor approval, then move into a numbered section here.

---

## D1. Root-cause selection is deterministic

The ranking of candidate root-cause services comes from the graph walk and the scoring formula.
The LLM **never** picks the root cause — it only writes the natural-language summary over the
evidence bundle the ranking produced.

**Trigger to revisit:** supervisor requests an LLM-as-judge ablation for the paper. Revisit, do
not reverse: any ablation that lets the LLM *pick* must be reported as a separate baseline, not as
a change to the system's output.

## D2. Dependency graph orientation and RCA walk direction

The service dependency graph is a NetworkX `DiGraph` with edges **caller → callee**. Failures
propagate **callee → caller**. RCA starts at the symptom service and walks **toward its callees**
(deeper into the graph). Do not use “upstream” without stating which direction you mean.

**Trigger to revisit:** if trace topology cannot be inferred reliably, document the fallback in
the change log and get supervisor sign-off.

## D3. No deep-learning training

Detection is statistical only (Z-score, EWMA, percentile, error-rate spike). No DL model is
trained inside InfraMind. The only learned component in the loop is the dependency graph's edge
weights, and those are computed from traces, not learned.

**Trigger to revisit:** never. Out of scope: D8.

## D4. Baselines freeze while an incident is open

While an incident is in the `OPEN` or `ACKNOWLEDGED` state, the detector does not update the
service's rolling baseline. Otherwise the fault gradually becomes "normal" and the detector
silently stops firing. Baselines resume updating after the incident transitions to `RESOLVED`
or after the configured decay window expires.

## D5. Test app is Online Boutique / OTel Demo, on kind

The system-under-test is Google's `microservices-demo` (Online Boutique) or the OpenTelemetry
Demo (`opentelemetry-demo` / Astronomy Shop). Sock Shop is **not** acceptable — its tracing is
too thin to exercise the Jaeger collector. The cluster is **kind** (Kubernetes-in-Docker), one
node, no cloud.

**Trigger to revisit:** if Online Boutique's repo is archived or its instrumentation is removed,
fall back to OTel Demo. Anything beyond that needs supervisor sign-off.

## D6. Evaluation: ≥12 distinct scenarios × 5 reps, plus a fault-free soak

Every scenario runs **5 times with a fresh seed**. Numbers in the paper are **mean ± std with a
95% confidence interval**. One **fault-free 30–60 minute soak run** exists solely to measure
false-alarm rate (false alarms per hour). The dev/test split is used for detector-weight tuning
only — final numbers come from the held-out test set.

## D7. Six baselines

| # | Baseline | What it does |
|---|---|---|
| 1 | **Raw Alertmanager** | Standard `kube-prometheus-stack` rules, unedited. The floor one can defend. |
| 2 | **Random** | Uniform random service. Sanity check. |
| 3 | **Highest-error-service** | The service with the highest error rate in the incident window. |
| 4 | **Deepest-erroring-span** | The deepest erroring leaf span in the trace. |
| 5 | **PageRank (MicroRCA-style)** | Personalized PageRank on the service-call graph seeded by anomalous services. |
| 6 | **LLM-only** | The LLM receives the same evidence bundle InfraMind produces and is asked to pick the root cause directly. Reported separately; not the system's choice. |

These are the comparison set. Adding or removing a baseline is a D-change.

## D9. LLM access is gated by redaction and a local fallback

Before any text leaves the process:

1. Secret-like strings (tokens, passwords, connection strings, bearer headers) are redacted.
2. Pod names, container images, and node names are hashed.
3. The LLM provider is `OPENAI` by default; **`OLLAMA`** is the local fallback (set
   `LLM_PROVIDER=OLLAMA`).

No raw log lines, env vars, or request bodies are sent to the LLM. The validator (see the LLM
explainer agent) re-checks that the LLM's output references entities present in the evidence.

## D12. Sidecars over a sidecar-less pod

Where the system-under-test needs instrumentation the upstream doesn't ship, InfraMind adds a
**sidecar** rather than forking the upstream image. Sidecar images live under
`testbed/sidecars/` and are version-pinned.

**Trigger to revisit:** if the upstream image ships an OpenTelemetry SDK natively, the sidecar
is removed and the change is recorded in the [change log](#change-log-adr-lite).

## D15. Evidence is bundled before ranking, not after

The RCA stage walks the graph from the symptom service toward its callees and **gathers
evidence per candidate** as it goes. Ranking happens on the bundle, never on a score computed
before the evidence is known. This keeps the score explainable and the LLM prompt stable.

## D20. Storage is the source of truth, the bus is the conduit

Postgres holds incidents, evidence, RCA results, and the append-only audit log. Redis Streams
is the conduit between stages — it is **not** a source of truth. If Postgres is unavailable,
the system refuses to acknowledge an incident rather than silently dropping it.

## D21. The service graph is a `ServiceGraph` primitive owned by the RCA worker

The service dependency graph is a single, pinned primitive called `ServiceGraph`. Its shape
is a `networkx.DiGraph` with edges `caller -> callee` (D2). The **authoritative live copy**
lives in-memory in the **RCA worker** (`src/inframind/rca/graph.py`); it is the only
consumer on the request path. The cold-start seed is `RCA_GRAPH_PATH` (default
`data/service-graph.json`). Live updates arrive from the OTel / Jaeger collector over a
**pinned Redis stream** named `stream:graph-updates` — a new pinned key alongside
`stream:signals` and `stream:incidents`.

Edges are weighted by `p95_latency_ms * log(call_count)`, exponentially weighted moving
average with α = 0.1 over the last 100 samples. An edge is added only after
`GRAPH_MIN_OBSERVATIONS` (default 3) observations. Edges that are not re-observed within
`2 * GRAPH_DECAY_HALF_LIFE_S` are removed. The on-disk snapshot is taken every 60 s and
on `SIGTERM`. A corrupt snapshot is logged at `ERROR` and replaced by the cold-start path;
the worker never crashes on a bad snapshot.

**Trigger to revisit:** if the project scales to multiple RCA replicas, the watcher
section ownership and the snapshot protocol change. That is a D-change.

## D22. `change_correlation` is the RCA `w4` term, sourced from a K8s API watch

The fourth term of the RCA scoring formula (section 6.4) is `w4 * change_correlation`. The
data source is a single K8s API watch on `Deployment`, `StatefulSet`, `DaemonSet`,
`ConfigMap`, `Secret`, and `HorizontalPodAutoscaler` resources, scoped to the namespaces
listed in `INFRAMIND_WATCH_NAMESPACES`. The watch is run in-process by the RCA worker pod
(no separate `Deployment`).

A change event captures metadata only: the resource kind, the resource name, the service
name (canonical), the revision, the previous revision, and the change time. **No `Secret`
value is ever stored, logged, or sent to the LLM** (D9). The watcher's `Role` extends the
existing read-only `Role` from section 11.5 with `secrets` `get` / `list` / `watch` only —
no write verbs, no `pods/exec`.

The correlation algorithm uses a 30-minute pre-window
(`CHANGE_CORRELATION_PRE_WINDOW_S`, default 1800 s) before `incident.opened_at`, with
recency-weighted soft saturation. The result is a float in `[0, 1]`. The
`change_events` Postgres table is append-only (D20); no `UPDATE`, no `DELETE`.

**Trigger to revisit:** if the project ever scales to multiple RCA replicas, the watcher
becomes a separate `Deployment` with one replica. That is a D-change.

## D23-Scripts. A new top-level `scripts/` directory is permitted for dev-time tooling

A top-level `scripts/` directory is permitted. It holds dev-time tooling — currently the
three pre-commit enforcement hooks (D24). The directory is **not part of the runtime**: it
is excluded from the Docker image, the Python package, the Helm chart, and the testbed
manifests. It is owned by the testbed-engineer (Rifat) by default; a different owner can be
set in `CODEOWNERS`.

**Trigger to revisit:** if a non-dev-time script ever needs to land under `scripts/` (e.g.
a runtime installer), the directory's scope changes. That is a D-change.

## D24-Hooks. Three pre-commit hooks enforce the standards

Three pre-commit hooks are required:

- **`check_imports.py`** enforces the dependency DAG (section 1.3). Per-line
  `# noqa: dag-violation` is the documented exemption, matching the existing `noqa`
  discipline.
- **`check_commit_message.py`** enforces the trailer ban (section 8.2.1). A Python
  allow-list of human GitHub usernames defines who can appear in a `Co-authored-by:`
  trailer; tooling (Cursor, Copilot, Claude, etc.) is always rejected.
- **`check_progress.py`** enforces the PROGRESS.md row requirement (section 8.4). A
  code-touching commit must touch `docs/PROGRESS.md` in the same commit, or the most
  recent row in `PROGRESS.md` must be dated within `PROGRESS_GRACE_DAYS` (default 7).

All three run locally (first line of defense) and in CI (second line). The rollout is
staged: each hook lands in `--simulate` mode for one week before flipping to enforce.

**Trigger to revisit:** adding a fourth hook or weakening an existing one is a D-change.

## D25-Author. The commit author is a GitHub username, never a display name, with no AI co-authorship

The `git config user.name` field on every commit in this repo is the **GitHub
username** of the human author — lowercase, exactly as it appears on the
user's GitHub profile. The `git config user.email` field is the matching
`noreply` email (`<username>@users.noreply.github.com`). Display names
(`"Md. Rifat Hossain"`, etc.) are **not** accepted in the `name` field.

The pinned allow-list of GitHub usernames on this repo:

- `rifatbond007` — Rifat (testbed, deploy, scripts)
- `moneem-07` — Moneem (ingestion, detection, correlation, tests)
- `promerayhan` — Prome (RCA, LLM, alerting, storage, API, evaluation)

The pinned set of **rejected** `Co-Authored-By:` trailer authors is every AI
tooling name that auto-appends trailers: `claude`, `claude-code`, `puku`,
`puku-cli`, `cursor`, `github-copilot`, `codex`, `jetbrains-ai`, and any name
matching `*<bot>*` or `*@users.noreply.github.com` whose local-part is a tool
brand. The `check_commit_message.py` pre-commit hook (D24) enforces both
rules; the allow-list is a Python list in that script and a PR to add or
remove a name goes through normal review.

**Why:** the audit log (`docs/PROGRESS.md` "who" column, `git log --format='%an %ae'`,
GitHub's contributor graph) must agree on who wrote what. A display name in
the `name` field is human-readable but unstable (it can be changed in
profile settings) and not what GitHub's noreply email guarantees. A
GitHub username is the only identity GitHub's noreply email proves, and it
is the identity the team recognises in PRs, code review, and CODEOWNERS.
The `Co-Authored-By:` rule exists because tool auto-trailers are noise: the
author is the human, and the tool's role is recorded in the commit body
when it is interesting (e.g. "Generated by Claude Code, reviewed by Rifat").

**Trigger to revisit:** adding a fourth team member, retiring one, or moving
to a richer authorship model (e.g. signed commits with GPG keys from
`@users.noreply.github.com` IDs) is a D-change.

## D-Setup. Step 0 only scaffolds the repo

Step 0 (this phase) creates the module skeletons, tooling, CI, and dev compose. No functional
ingestion, detection, RCA, LLM, alerting, or storage code lands in this phase. The K8s testbed
(kind + observability stack + Chaos Mesh) is Step 1 work and lives behind `make up`.

## Out of scope (D8, repeated here for the doc)

No cloud, no VMs, no Datadog/PagerDuty, no auto-remediation, no production scale, no DL training,
no security-fault injection. If a piece of work appears to need any of these, stop and ask.

---

## Change log (ADR-lite)

Format: **ID · date · decision · why · alternatives · status** (`proposed` / `approved by supervisor`)

### Approved

- **D-Setup** · 2026-10-05 · Step 0 only creates skeletons and tooling; no functional code in this phase. K8s testbed lives in `make up` (Step 1). docker-compose covers only Redis and PostgreSQL. Functional code lands phase by phase per `docs/PROGRESS.md`. · Why: verifiable scaffold without violating D3/D5 prematurely. · Alternatives: full stack in Step 0 (rejected). · Status: approved by the team; supervisor follow-up in next sync.

- **D21** · 2026-10-11 · The service dependency graph is a single pinned primitive called `ServiceGraph`, owned by the RCA worker (one replica), with edges `caller -> callee`, weighted by `p95_latency_ms * log(call_count)` over the last 100 samples (α = 0.1 EWMA). Cold-start seed from `RCA_GRAPH_PATH`. Live updates from the OTel / Jaeger collector over the pinned Redis stream `stream:graph-updates`. Edges age out via `GRAPH_DECAY_HALF_LIFE_S`; an edge below `GRAPH_MIN_OBSERVATIONS` is held back. Snapshot every 60 s and on `SIGTERM`; corrupt snapshot -> cold start, never a crash. · Why: the graph is consumed by the RCA walker, the PageRank baseline, the correlation severity classifier, and the LLM evidence bundler — six call sites in total. Without a pinned primitive, every consumer invents its own, and a deviation is a silent scoring bug. The single-writer / single-reader model also satisfies the D6 reproducibility contract: same `scenario_id + seed` -> same `GraphUpdate` sequence -> same in-memory graph -> same ranking. · Alternatives: (a) rebuild the graph on every incident from raw traces — rejected for hot-path latency (1–5 ms per request). (b) Reuse `stream:signals` for graph updates — rejected because `GraphUpdate` has a different shape and a different consumer lifecycle. (c) Store the live copy in Postgres — rejected because the ranker needs O(1) edge access, not a SQL round-trip. · Status: approved by team (supervisor sign-off pending next sync). Cross-ref: `docs/standards/13-service-graph-contract.md`.

- **D22** · 2026-10-11 · `change_correlation` is the pinned algorithm for the RCA `w4` term. Source is a single in-process K8s API watch on `Deployment`, `StatefulSet`, `DaemonSet`, `ConfigMap`, `Secret`, `HorizontalPodAutoscaler` (filtered to `.spec.template`, `.data`, `.spec.replicas` respectively), scoped to `INFRAMIND_WATCH_NAMESPACES`. Captures metadata only — **no Secret values stored**. Correlation uses a 30-minute pre-window (`CHANGE_CORRELATION_PRE_WINDOW_S`) before `incident.opened_at`, with recency-weighted soft saturation, returning a float in `[0, 1]`. The `change_events` Postgres table is append-only (D20). The watcher's `Role` extends the existing read-only `Role` (section 11.5) with `secrets` `get` / `list` / `watch` only — no write verbs, no `pods/exec`. · Why: the `w4` term is named and weighted in section 6.4 but had no algorithm. Without a pinned spec, every implementation invents its own and the D6 reproducibility contract breaks at the `w4` term. The K8s API watch (not the audit log) matches the existing read-only RBAC scope with a minimal extension. · Alternatives: (a) Watch the audit log — rejected for being more privileged than needed. (b) Poll the K8s API on a timer — rejected because polling misses bursts of changes between polls. (c) Use a sidecar — rejected because it adds a new `Deployment` with its own lifecycle for a single consumer. · Status: approved by team (supervisor sign-off pending next sync). Cross-ref: `docs/standards/14-change-correlation.md`.

- **D23-Scripts** · 2026-10-11 · A new top-level `scripts/` directory is permitted for dev-time tooling only. Currently holds the three pre-commit enforcement hooks (D24). Excluded from the Docker image, the Python package, the Helm chart, and the testbed manifests. Owned by testbed-engineer (Rifat) by default. · Why: the hook scripts are real Python with logic that deserves version control and a README. The alternative (inline as bash in `.pre-commit-config.yaml`) is unmaintainable past ~30 lines. Section 1.6 requires a D-number for any new top-level directory; this is the number. · Alternatives: (a) Hide the hooks under `.githooks/` — rejected because hidden dev-time tooling rots. (b) Put them under an existing dir (`tools/`, `dev/`) — rejected because they are scripts, not tools, and the names are misleading. · Status: approved by team (supervisor sign-off pending next sync). Cross-ref: `docs/standards/15-enforcement-hooks.md` section 15.3.

- **D24-Hooks** · 2026-10-11 · Three pre-commit hooks are required: `check_imports.py` (enforces the dependency DAG, section 1.3), `check_commit_message.py` (enforces the trailer ban, section 8.2.1), `check_progress.py` (enforces the PROGRESS.md row requirement, section 8.4). All three run locally (first line) and in CI (second line). Rollout is `--simulate` mode for one week per hook before enforcing. Per-line `# noqa: dag-violation` is the documented DAG exemption. The `Co-authored-by:` allow-list is a Python list in `check_commit_message.py` — adding a human co-author is a PR that updates the script. `PROGRESS_GRACE_DAYS` defaults to 7. · Why: the standards have failed silently in past projects because they were enforced by reviewer discipline, not by tooling. The three rules in scope here (DAG, trailers, PROGRESS.md) are the ones that have failed. · Alternatives: (a) Enforce all at once with no rollout — rejected; rollout with `--simulate` is the proven way to surface false positives without blocking the team. (b) Use a third-party hook framework (pre-commit-hooks) — rejected; the rules are InfraMind-specific. · Status: approved by team (supervisor sign-off pending next sync). Cross-ref: `docs/standards/15-enforcement-hooks.md` sections 15.4–15.6.

- **D25-Author** · 2026-10-10 · The commit author is a **GitHub username** (lowercase, e.g. `rifatbond007`), never a display name. The `user.email` is the matching `noreply` email (`<username>@users.noreply.github.com`). No `Co-Authored-By:` trailer for any AI tooling (Claude Code, Puku-CLI, Cursor, GitHub Copilot, Codex, JetBrains AI, etc.); the trailer form is reserved for the rare GitHub-UI-generated human pair-commit. Pinned allow-list of humans: `rifatbond007`, `moneem-07`, `promerayhan`. · Why: the audit log (`docs/PROGRESS.md` "who" column, `git log --format='%an %ae'`, GitHub's contributor graph) must agree on who wrote what. The `noreply` email guarantees the username, not the display name; a display name can be changed in profile settings and is not what GitHub proves. Tool auto-trailers are noise: the author is the human, the tool's role is in the body when it is interesting. The hook `check_commit_message.py` (D24) enforces this with a Python list of allowed `Co-authored-by:` local-parts and rejects display-name authors via `git log -1 --format='%an %ae'`. · Alternatives: (a) Keep the display name and rely on the email — rejected because the `noreply` email does not attest to a display name, only a username. (b) Allow `Co-Authored-By:` for tools but mark them as such — rejected because the trailer is hard to filter in tooling and the audit log just gets noisier. (c) Use GPG-signed commits instead — rejected for this capstone; the GitHub username + noreply email pair is sufficient. · Status: approved by team (supervisor sign-off pending next sync). Cross-ref: `docs/standards/08-commit-protocol.md` section 8.2.1; `.claude/agents/quality/code-reviewer.md`.

### Proposed deviations from the July 2026 proposal (supervisor approval pending)

These are already reflected in `CLAUDE.md` and implementation docs; status moves to **approved**
when the supervisor signs off.

- **D2 (wording)** · Proposal used ambiguous “upstream”; repo locks caller→callee and walk toward callees (see [D2](#d2-dependency-graph-orientation-and-rca-walk-direction) above).
- **D5** · Online Boutique / OTel Demo instead of Sock Shop.
- **D6** · 5 runs per scenario (proposal: 3); fault-free soak; FPR = false alarms/hour on soak.
- **D7** · Stronger RCA baselines + LLM-only baseline.
- **NFR** · Throughput targets (10k logs/s, 5k spans/s) → load-test numbers on the actual machine.
- **Timeline** · Phase 9 (paper, 3–4 weeks); re-baseline dates from real start date.

---

## How a new decision lands

1. Open a `docs/` PR with a new [change log](#change-log-adr-lite) entry (date, change, trigger, sign-off).
2. Update this file if the decision is durable — give it the next `D-NN` section.
3. If the decision changes a public contract (a signal schema, an API endpoint, a scenario
   shape), update `ARCHITECTURE.md` and the relevant module's `README.md` in the same PR.
4. Decisions touching RCA, evaluation, or the testbed need supervisor sign-off before merge.

## How to read this file

Read top-to-bottom on first contact. After that, jump by ID — every other doc links by ID, never
by paraphrase.
