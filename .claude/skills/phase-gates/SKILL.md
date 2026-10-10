---
name: phase-gates
description: What must be true before moving from one phase to the next in InfraMind. Each gate is a checklist, not a vibe.
---

# Phase gates

You cannot start phase N+1 until phase N's gate is green.

A new gate applies to **every** phase transition, including this one: if a
PR changes a standard under `docs/standards/` or adds a D-NN to
`docs/decision-tree.md`, the agent file(s) that own the affected code are
updated in the **same PR**. A PR that changes a standard but leaves the
matching agent file stale is incomplete; the gate is red. This rule is
owned by the `project-planner` agent (D-change discipline).

## P0 → P1 (repo → testbed)

- [ ] `pyproject.toml`, `Makefile`, CI green.
- [ ] `docker-compose.yml` brings up Redis + Postgres.
- [ ] `docs/PROGRESS.md` has a Step-0 row.
- [ ] GitHub repo created; branch protection on `main` enabled.
- [ ] `CODEOWNERS` populated with real handles.

## P1 → P2 (testbed → schema)

- [ ] `kind` cluster boots in ≤ 5 minutes.
- [ ] kube-prometheus-stack, Loki, Jaeger, Chaos Mesh, Online Boutique all healthy.
- [ ] Smoke: inject pod-kill, confirm Prometheus, Loki, Jaeger all see it.

## P2 → P3 (schema → ingestion)

- [ ] `Signal` Pydantic model committed.
- [ ] `Redis Streams` event bus wired with idempotent consumer.
- [ ] Unit tests for `Signal` validation pass.

## P3 → P4 (ingestion → detection)

- [ ] At least Prometheus + Alertmanager collectors end-to-end.
- [ ] Loki + OTel collectors surface sync.
- [ ] The Jaeger / OTel collector emits `GraphUpdate` events on the pinned
      Redis stream `stream:graph-updates` (D21). The graph itself is owned by
      the RCA worker; ingestion does not mutate it.

## P4 → P5 (detection → RCA)

- [ ] Z-score detector fires on a synthetic spike.
- [ ] EWMA detector fires on a synthetic spike.
- [ ] Baseline freeze works while an incident is open.
- [ ] End-to-end: anomaly → incident.

## P5 → P6 (RCA → alerting/storage)

- [ ] Deterministic ranker ranks a known scenario correctly on the dev split.
- [ ] PageRank variant implemented.
- [ ] LLM validator rejects fake evidence IDs.
- [ ] Ollama fallback works without `OPENAI_API_KEY`.

## P6 → P7 (alerting → evaluation)

- [ ] Postgres schema migrated.
- [ ] Alert emits end-to-end with full RCA payload.
- [ ] Audit log records every transition.
- [ ] `/healthz`, `/metrics`, `/incidents` endpoints respond.

## P7 → P8 (evaluation → packaging)

- [ ] ≥12 scenarios × 5 runs + fault-free soak on the dev split.
- [ ] All 6 baselines (D7) implemented.
- [ ] Ablation: no-correlation, no-RCA, no-LLM variants.
- [ ] Test split runs; numbers saved to CSVs.

## P8 → P9 (packaging → paper)

- [ ] `make up` brings up the full stack on a fresh machine.
- [ ] Helm chart lints; image builds.
- [ ] README quickstart works.

## P9 → done (paper)

- [ ] All references verified by `reference-verifier`.
- [ ] Numbers in paper traced to CSVs.
- [ ] Supervisor approves the camera-ready.
- [ ] Submission accepted (or paper ready for the next venue).
