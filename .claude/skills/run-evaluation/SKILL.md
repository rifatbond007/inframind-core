---
name: run-evaluation
description: How to run the evaluation harness — single scenario, full matrix, soak run, baselines.
---

# Run evaluation (skill entrypoint)

**Canonical procedure:** [`docs/standards/10-evaluation.md`](../../../docs/standards/10-evaluation.md)

Before running or changing the harness:

1. Read [`docs/PROGRESS.md`](../../../docs/PROGRESS.md) — evaluation is Phase P7.
2. Use `make eval SCENARIO=<id>` / `make eval-all` when implemented; until then follow the standards manual steps.
3. Never tune on the test split; never invent metrics — write CSVs under `evaluation/results/`.

Owner: evaluation-engineer agent (`Rifat`).
