---
name: fault-scenario
description: How to author a fault-injection scenario YAML for InfraMind evaluation (Chaos Mesh + ground truth).
---

# Fault scenario (skill entrypoint)

**Canonical procedure:** [`docs/standards/10-evaluation.md`](../../../docs/standards/10-evaluation.md) (scenario authoring sections)

Before adding a scenario:

1. Place the file at `evaluation/scenarios/<dev|test>/NN-<kind>-<service>-<fault>.yaml`; use Online Boutique names (`frontend`, `checkoutservice`, ...) and namespace `default`; `expected_affected_services` = target + its transitive callers (the loader test derives them from the manifests).
2. Include reproducible `seed`, ground-truth root cause, and a Chaos Mesh spec the loader accepts (PodChaos `pod-kill`/`pod-failure`/`container-kill`, StressChaos `stressors`, NetworkChaos actions) or a `config_change` block.
3. Probe the fault live once (`AllInjected=True`) before marking it `runnable`; a server dry run is not enough. Keep (fault_type, service) unique across dev and test.
4. Log the new scenario in `docs/PROGRESS.md`.

Owner: evaluation-engineer agent (`Rifat`).
