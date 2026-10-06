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
