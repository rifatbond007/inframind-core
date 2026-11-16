---
name: run-evaluation
description: How to run the evaluation harness — single scenario, full matrix, soak run, baselines.
---

# Run evaluation (skill entrypoint)

**Canonical procedure:** [`docs/standards/10-evaluation.md`](../../../docs/standards/10-evaluation.md)

Before running or changing the harness:

1. Read [`docs/PROGRESS.md`](../../../docs/PROGRESS.md) — evaluation is Phase P7.
2. Record live fixtures with `make eval-live [EVAL_SET=dev|test|all] [SCENARIO=<id>]` (needs `make up` and the load generator at 1 replica; ~15 min per repeat; run it detached). Score by replay: `python -m inframind.evaluation.runner.main --set dev --scenario <id>` with `INFRAMIND_EVAL_RESULTS_DIR` pointing at a scratch dir until results are meant to be kept.
3. Read the logs for `fixture_dirty_baseline`, `fixture_no_fault`, `baseline_not_run`: each means a row was withheld, never faked.
4. Never tune on the test split; never invent metrics — write CSVs under `evaluation/results/`.

Owner: evaluation-engineer agent (`Rifat`).
