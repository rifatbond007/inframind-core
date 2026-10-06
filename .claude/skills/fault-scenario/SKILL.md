---
name: fault-scenario
description: How to author a fault-injection scenario YAML for InfraMind evaluation (Chaos Mesh + ground truth).
---

# Fault scenario (skill entrypoint)

**Canonical procedure:** [`docs/standards/10-evaluation.md`](../../../docs/standards/10-evaluation.md) (scenario authoring sections)

Before adding a scenario:

1. Place files under `evaluation/scenarios/<id>/`.
2. Include reproducible `seed`, ground-truth root cause service, and Chaos Mesh spec.
3. Log the new scenario in `docs/PROGRESS.md`.

Owner: evaluation-engineer agent (`Rifat`).
