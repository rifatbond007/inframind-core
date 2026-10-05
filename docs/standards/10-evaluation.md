# 10. Evaluation — single scenario, full matrix, soak, baselines, ablations
> standards · Process · InfraMind. Pairs with the `run-evaluation` and `fault-scenario` skills in `.claude/skills/`. This file is the rationale.

## 10.1 The shape

```
evaluation/
├── scenarios/
│   ├── dev/                    # for tuning
│   └── test/                   # for the final report
├── runner/                    # applies Chaos Mesh CRs, records, cleans up
├── baselines/                 # the 6 rankers compared against InfraMind
├── scoring/                   # metrics + plots + 95% CI
├── notebooks/                 # Jupyter analysis (kept small; results go in results/)
└── results/                   # generated CSVs / figures (gitignored except .gitkeep)
```

The split is sacred. The dev split is for tuning; the test split is for the report.

## 10.2 Single scenario

```bash
make eval SCENARIO=001-pod-kill-frontend
```

This:

1. Loads `evaluation/scenarios/{dev,test}/<scenario_id>/scenario.yaml`.
2. Applies the Chaos Mesh CR (`kubectl apply --context kind-inframind`).
3. Waits `warmup_s`, runs for `duration_s`, waits `cooldown_s`.
4. Captures InfraMind's incident record from Postgres.
5. Cleans up the Chaos Mesh CR.
6. Computes the metrics and writes `evaluation/results/<scenario_id>/run_<n>.csv`.

The runner never inspects the testbed's state mid-run beyond the recorded step. If the runner
fails, the partial CSV is preserved under `evaluation/results/<scenario_id>/partial/` and the
testbed is cleaned up before exit.

## 10.3 Full matrix

```bash
make eval-all
```

Runs every scenario `repeats` times (5 by default; D6). Outputs:

- `evaluation/results/<scenario_id>/run_<n>.csv` per scenario × repeat.
- `evaluation/results/main.csv` — aggregated.
- `evaluation/results/ci_report.txt` — 95% CIs.

`eval-all` is the paper's source of truth. Anything that does not appear in `main.csv` does
not appear in the paper.

## 10.4 Soak run (fault-free)

```bash
make eval-soak DURATION=30m
```

For 30 minutes, no fault is injected. The system is left alone. The output measures **false
alarms per hour** — incidents opened that should not have been. The soak run is required by
D6; the paper reports it as a separate row.

## 10.5 The six baselines (D7)

| # | Baseline | What it does |
|---|---|---|
| 1 | Raw Alertmanager | Standard `kube-prometheus-stack` rules, unedited. The floor. |
| 2 | Random | Uniform random service. Sanity check; should be worst. |
| 3 | Highest-error-service | The service with the highest error rate in the window. |
| 4 | Deepest-erroring-span | The deepest erroring leaf span in the trace. |
| 5 | PageRank (MicroRCA-style) | Personalised PageRank on the call graph, seeded by anomalous services. |
| 6 | LLM-only | The LLM receives the same evidence bundle and picks the root cause directly. Reported as a separate ablation, not as InfraMind's choice. |

These are the comparison set. Adding or removing a baseline is a D-change.

## 10.6 Ablations

```bash
make eval-ablation SCENARIO=001-pod-kill-frontend
```

Runs the scenario with one component off:

| Variant | What changes |
|---|---|
| `no-correlation` | Skip the correlation stage; every anomaly is its own incident. |
| `no-rca` | Skip the graph stage; the LLM picks. |
| `no-llm` | Use a deterministic template summary, not the LLM. |
| `no-change-correlation` | The `change_correlation` term in the score formula is zeroed. |

Ablations measure each contribution. The paper reports the headline result with all
contributions on, and then a table of ablations.

## 10.7 The dev/test split

- **Tune on dev.** Detector thresholds, scorer weights, the `max_depth` in the walk, anything
  that has a free parameter — tuned on `evaluation/scenarios/dev/`.
- **Report on test.** The paper's numbers come from `evaluation/scenarios/test/` only.
- **Never shuffle.** The split is locked at the start of P7. Re-shuffling invalidates prior
  runs and is rejected.

## 10.8 The schema for one scenario

```yaml
scenario_id: "001-pod-kill-frontend"
seed: 42
description: "Kill one frontend pod and observe RCA."

fault_type: "pod_kill"
target_service: "frontend"
target_namespace: "default"
duration_s: 300
expected_root_cause: "frontend"
expected_affected_services:
  - "frontend"
  - "checkoutservice"

chaos:
  kind: "PodChaos"
  spec:
    action: "pod-kill"
    mode: "one"
    selector:
      namespaces: ["default"]
      labelSelectors:
        app: "frontend"

warmup_s: 60
cooldown_s: 120

metrics_to_assert:
  - "MTTD < 60s"
  - "RCA top-1 = frontend"
repeats: 5
```

Every scenario is reproducible from `scenario_id + seed`. The `seed` is the field that
pins every random choice — Chaos Mesh pod selection, traffic load generator, anything with
a `random.random()`.

## 10.9 Required scenario coverage (locked)

| # | Fault |
|---|---|
| 1 | Service crash (pod-kill) |
| 2 | Pod restart loop (CrashLoopBackOff) |
| 3 | CPU stress |
| 4 | Memory pressure |
| 5 | Network delay |
| 6 | Network packet loss |
| 7 | High HTTP 500 error rate |
| 8 | Slow downstream service |
| 9 | Database unavailable |
| 10 | Misconfigured environment variable (`kubectl set env`) |
| 11 | Sudden log error spike |
| 12 | Trace latency increase |
| + | Multi-fault (two simultaneous faults) |
| + | Fault-free soak run (30–60 min) |

## 10.10 Metrics

| Metric | What it measures |
|---|---|
| MTTD | Mean time to detect — fault injection time to first `Anomaly`. |
| MTTDg | Mean time to detection (ground-truth-anchored) — same, with the GT as the start. |
| Alert precision | Of alerts sent, how many pointed at the GT root cause. |
| Alert recall | Of all faults, how many produced an alert pointing at the GT root cause. |
| RCA top-1 accuracy | Did the deterministic ranker output the GT service as the top candidate? |
| RCA top-3 accuracy | Was the GT in the top 3? |
| RCA latency | From `Anomaly` group → ranked candidates with evidence. |
| False alarms / hour | Soak run only. |

Each metric comes from a CSV in `evaluation/results/`. The paper's tables cite the CSV
column.

## 10.11 Hard rules

- **D6: ≥12 scenarios × 5 runs + fault-free soak.** Numbers in the paper come only from this
  matrix.
- **D6: dev/test split.** Never tune on test.
- **D7: 6 baselines.** Adding or removing a baseline is a D-change.
- **Never invent a number.** Every result is from `evaluation/results/`.
- **The scenario YAML has a `seed`.** No scenario without a seed.

## 10.12 Cross-reference

- **Skills:** `.claude/skills/run-evaluation/SKILL.md`, `.claude/skills/fault-scenario/SKILL.md`.
- **Locked decisions:** D6 (matrix), D7 (baselines).
- **Paper writing:** `12-paper-writing.md` — where these numbers land.
- **Results → LaTeX:** `.claude/skills/results-to-latex/SKILL.md` — the table/figure pipeline.
