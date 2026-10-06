---
name: testbed-ops
description: Operational runbook for the InfraMind testbed — kind cluster, observability stack, Chaos Mesh, smoke checks.
---

# Testbed ops (skill entrypoint)

**Canonical procedure:** [`docs/standards/09-testbed-ops.md`](../../../docs/standards/09-testbed-ops.md)

Before touching `testbed/` or cluster commands:

1. Read [`docs/PROGRESS.md`](../../../docs/PROGRESS.md) — testbed is Phase P1.
2. Use `kubectl` / `helm` only against `kind-*` contexts (hard rule in `CLAUDE.md`).
3. Run the smoke checklist in the standards file after changes.

Owner: testbed-engineer agent (`Rifat`).
