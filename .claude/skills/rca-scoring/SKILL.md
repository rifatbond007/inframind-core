---
name: rca-scoring
description: How to implement and tune the root-cause ranking in InfraMind (deterministic graph walk + PageRank variant).
---

# RCA scoring (skill entrypoint)

**Canonical procedure:** [`docs/standards/06-rca-scoring.md`](../../../docs/standards/06-rca-scoring.md)

Before changing ranking or evidence bundling:

1. Read [`docs/PROGRESS.md`](../../../docs/PROGRESS.md) — RCA is Phase P5.
2. Enforce D1 (deterministic rank) and D2 (caller→callee, walk toward callees).
3. Compare weighted formula vs PageRank on the **dev** split only; log in `docs/PROGRESS.md`.
4. D30 (PROPOSED): observed failing calls (`call_error` anomalies' `caller`) are walkable edges for the incident, and a candidate keeps its deepest reach from any symptom. Re-check the caller-bug case (dev-11) before changing either.

Owner: rca-engineer agent (`Prome`).
