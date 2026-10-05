---
name: run-evaluation
description: How to run the evaluation harness — single scenario, full matrix, soak run, baselines, ablations.
---

# Run evaluation

The evaluation harness applies Chaos Mesh CRs, records InfraMind's output, cleans up, and scores the result.

## Single scenario

```bash
make eval SCENARIO=001-pod-kill-frontend
```

This:
1. Loads `evaluation/scenarios/001-pod-kill-frontend/scenario.yaml`.
2. Applies the Chaos Mesh CR.
3. Waits `warmup_s`, runs for `duration_s`, waits `cooldown_s`.
4. Captures InfraMind's incident record from Postgres.
5. Cleans up the Chaos Mesh CR.
6. Computes the metrics and writes to `evaluation/results/001-pod-kill-frontend/run_<n>.csv`.

## Full matrix

```bash
make eval-all
```

Runs every scenario `repeats` times. Outputs:
- `evaluation/results/<scenario_id>/run_<n>.csv` per scenario × repeat.
- `evaluation/results/main.csv` aggregated.
- `evaluation/results/ci_report.txt` with 95% CIs.

## Soak run (fault-free)

```bash
make eval-soak DURATION=30m
```

For 30 minutes, no fault is injected. Measures false alarms per hour.

## Baselines

```bash
make eval-baselines SCENARIO=001-pod-kill-frontend
```

Runs the same scenario through every baseline ranker:
- raw Alertmanager (with standard kube-prometheus-stack rules).
- random.
- highest-error-service.
- deepest-erroring-span.
- PageRank (MicroRCA-style).
- LLM-only.
- InfraMind (this paper's method).

## Ablation

```bash
make eval-ablation SCENARIO=001-pod-kill-frontend
```

Runs the scenario with one component disabled:
- no correlation.
- no RCA graph (LLM picks).
- no LLM (template summary).
- no change correlation.

## Outputs

- Per-scenario CSVs under `evaluation/results/<id>/`.
- `evaluation/results/main.csv` — aggregated metrics.
- `evaluation/results/ci_report.txt` — 95% CIs.
- `evaluation/results/figures/` — matplotlib figures (PNG + PDF).

## Hard rules

- Dev / test split: tune on `evaluation/scenarios/dev/`, report on `evaluation/scenarios/test/`. Don't mix.
- Never invent a number. Every result comes from a CSV in `evaluation/results/`.
- Never tune thresholds on the test split.