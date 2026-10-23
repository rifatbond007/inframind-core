# 10. Evaluation — replay harness, baselines, ablations
> standards · Process · InfraMind. Pairs with the `run-evaluation` skill in `.claude/skills/`.
> P7 replaces the P7 *intent* with the **implemented harness**.

## 10.1 The shape

```
src/inframind/evaluation/
├── __init__.py         public surface: baselines, ablations, schema, scoring, runner
├── config.py           EvalParams, load_eval_params — INFRAMIND_EVAL_* env keys
├── schema.py           ChaosSpec, ScenarioSpec, ResultRow, FixtureMeta (all frozen)
├── scenarios.py        YAML loader, dev/test split enforcement, blocked marker
├── scoring.py          top-1 / top-3 / MRR / latency / FPR; MethodAggregate
├── ablations.py        no-correlation / no-rca / no-llm / no-change-correlation
├── report.py           write_all: CSVs + summary.json + README, refuses unprovenanceed rows
├── baselines/          six D7 baselines; Baseline protocol; BaselineInput
└── runner/
    ├── replay.py       NDJSON fixture → determinstic pipeline → ResultRow
    ├── live.py         kubectl path; asserts kind-* context; refuses blocked scenarios
    └── main.py         CLI: --set, --dry-run, --soak, --ablation, --allow-blocked

evaluation/
├── scenarios/
│   ├── dev/            8 YAMLs (4 runnable + 4 blocked on D2-Chaos-runtime)
│   └── test/           6 YAMLs (4 runnable + 2 blocked on D2-Chaos-runtime)
├── fixtures/<scenario>/
│   ├── signals.ndjson
│   ├── graph.json
│   └── meta.json
└── results/phase7/     no numbers committed; README.md + .gitkeep only
```

The DAG permits `evaluation` to import `common`, `correlation`, `detection`, `rca`, `llm`
— never `alerting`, `storage`, or `api` (asserted by `tests/unit/test_module_dag.py`).

## 10.2 Reproducibility — replay, not wall-clock replay

The pipeline is signal-time-driven (D6 property, locked as **D7-Replay** in
`docs/decision-tree.md`): given the same `Signal` stream at the same timestamps,
detection, correlation, and ranking are deterministic. So a run is a **pure function of
a fixture**.

`runner/replay.py` exposes:

- `replay_signals(path)` — strict parse, asserts ts ordering with `itertools.pairwise`.
- `load_graph_snapshot(path)` — deserialises a `ServiceGraph` from `graph.json`.
- `compute_fault_window(onset_ts, duration_s)` — the time bounds the runner scores against.
- `replay_run(fixture_dir, *, spec, scorer, method, method_kind, run_id, seed, repeat, ...)`
  — feeds the fixture through detection → correlation → ranking, returns one
  `ResultRow`.
- `_tune_correlation(params, *, window_s)` — widens the correlation window to at least
  the `window_s` passed in, so a fixture cannot be undiagnosable just because the
  default window is shorter than the fault duration.

`runner/live.py` is the cluster path. It:

1. Asserts the kubeconfig context starts with `kind-` (PUKU.md rule — no `~/.kube/config`).
2. Refuses a blocked scenario unless `INFRAMIND_EVAL_ALLOW_BLOCKED=1`.
3. Refuses to run on a non-`kind-` context even with the flag. No escape hatch that can
   point at a shared cluster.
4. `run_scenario` raises `NotImplementedError` until the recorder lands — see 10.13.

The harness **ships unexecuted** on the live path. The replay integration test is the
real gate.

## 10.3 Scenarios — 14 YAMLs across the sacred split

`evaluation/scenarios/dev/` (8 files, tuning) and `evaluation/scenarios/test/` (6 files,
scored once). Each YAML:

```yaml
scenario_id: dev-podchaos-01-cartservice-pod-kill
set: dev
seed: 1001
fault_type: pod_kill
target_service: cartservice
target_namespace: boutique
duration_s: 180
warmup_s: 60
cooldown_s: 60
expected_root_cause: cartservice
expected_affected_services: [cartservice, front-end, checkout]
chaos:
  kind: PodChaos
  spec: {action: pod-kill, mode: one, selector: {label: app, value: cartservice}}
metrics_to_assert: [http_requests_total, http_request_duration_seconds_bucket]
repeats: 5
```

`chaos` may be omitted (e.g. the config-change test scenario uses a real `ConfigMap`
edit, not a chaos CR). `ScenarioSpec.is_blocked` returns `True` when `status: blocked`
or `block_status: <reason>` is present in the file, and `fixture_dirname` is the
canonical subdirectory of `evaluation/fixtures/`.

A malformed scenario is a **load error at import time**, not a silent skip — a scenario
you meant to run that never loaded is worse than a crash.

### The dev/test split is enforced in code

- `scenarios.parse_scenario` rejects a file whose `set` field disagrees with the
  directory.
- `scenarios.load_set("dev")` refuses a file declaring `set: test` (and vice versa).
- `scenarios.assert_disjoint(dev, test)` is the test that makes the split sacred.
- `EvalParams(scenario_set="test")` is allowed; the harness never refuses it. What it
  refuses is **tuning on the test set** — every dev path runs on `load_set("dev")` and
  the report author must consciously choose `eval-all` or `--set test`.

### Coverage against the 12 locked scenarios (D6)

| Scenario | Fault | Chaos | Status |
|---|---|---|---|
| dev-01..03 | pod-kill / container-exit / cpu-stress | PodChaos | runnable |
| dev-04..05 | cpu-stress / memory-stress | StressChaos | blocked — D2-Chaos-runtime |
| dev-06 | latency-inject | StressChaos | blocked — D2-Chaos-runtime |
| dev-07 | network-partition | NetworkChaos | blocked — D2-Chaos-runtime |
| test-01..03 | pod-kill / container-exit / node-cordon | PodChaos | runnable |
| test-04 | cpu-stress | StressChaos | blocked — D2-Chaos-runtime |
| test-05 | network-latency | NetworkChaos | blocked — D2-Chaos-runtime |
| test-06 | config-change (`kubectl set env`/ConfigMap) | none | runnable |

The blocked files carry `# block_status: d2_chaos_runtime_docker` and
`status: blocked`. The runner **refuses** them by default (exit code 3, message in
stderr). With `--allow-blocked`, the runner still runs but emits rows with
`blocked=True` — never a fabricated accuracy.

## 10.4 ResultRow — the frozen result schema

One `ResultRow` per `(scenario_id, seed, method)`. Frozen because P9 LaTeX tables read
these fields directly and a rename becomes a paper bug.

| Field | Type | Meaning |
|---|---|---|
| `scenario_id` | str | The YAML's `scenario_id`. |
| `seed` | int | The seed that ran. ULID `run_id` ties back to the raw fixture. |
| `repeat` | int | 0..`repeats-1` for that seed. |
| `method` | str | `infra` \| baseline name \| ablation name. |
| `detected` | bool | Did an incident open within the window? |
| `top1_correct` | bool | Predicted service == `expected_root_cause`. |
| `top3_correct` | bool | Expected is in the top 3. |
| `reciprocal_rank` | float | `1/rank` of the expected service, 0.0 if absent. |
| `rank` | int | 0 if absent. |
| `detection_latency_s` | `float \| None` | Fault onset → first incident-open publish. |
| `false_positive` | bool | Incident opened during the fault-free window. |
| `scenario_set` | str | `dev` \| `test`. |
| `run_id` | str | Pinned; ties the row back to the raw fixture. |
| `expected_root_cause` | str | Carried for the report writer. |
| `predicted_root_cause` | `str \| None` | Top-ranked service or `None` if undetected. |
| `blocked` | bool | `True` when the scenario was a blocked one. |

`scoring.score(rows)` aggregates to `MethodAggregate`:

| Field | Meaning |
|---|---|
| `count` | number of `ResultRow` in the bucket |
| `top1`, `top3`, `mrr` | means over the bucket |
| `detected_rate` | `mean(detected)` |
| `median_latency_s` | `None` if all rows undetected, not 0.0 |
| `false_positive_rate` | `mean(false_positive)` |

`default_scorer(spec, seed, repeat, *, method, method_kind, run_id, detected, ranked,
detection_latency_s, false_positive, blocked)` is the seam that turns a ranked list into
a scored row.

## 10.5 The six baselines (D7)

| # | Baseline | What it does | Why it is in the set |
|---|---|---|---|
| 1 | `raw_alertmanager` | Fires on the raw alert's `instance` label — no graph, no correlation. | The "what do we already have" floor. |
| 2 | `random_service` | Uniform pick from the service set, seeded. | The chance floor. Proves top-1 > chance. |
| 3 | `highest_error_service` | Highest error *rate* from metrics. | The obvious heuristic an SRE writes first. |
| 4 | `deepest_error_span` | Deepest erroring span in the trace graph. | The trace-only heuristic; RCA's closest competitor. |
| 5 | `pagerank` | **Re-export** of the P5 `rca.pagerank` implementation. | Proves the graph term earns its keep. |
| 6 | `llm_only` | `llm.explain()` output parsed for a service name. | D1's control: the LLM alone must score near chance. If it scores high, that is a finding about the evaluation, not a reason to change the code. |

`BASELINE_NAMES` is the locked tuple. `Baseline` is a `Protocol` with one method:
`rank(baseline_input, summary_text='') -> list[str]`. `BaselineInput` is frozen; an empty
service set raises `EmptyServiceSet`, never an empty ranking.

`llm_only` importing `llm` is allowed by the DAG. It needs no fixtures beyond the signal
stream — its only input is the LLM summary text plus the `BaselineInput`.

## 10.6 Ablations

| Variant | What it changes |
|---|---|
| `no-correlation` | `CorrelationParams.enabled = False`. Every anomaly is its own incident. |
| `no-rca` | Graph term zeroed; `low_confidence_multiplier` reverts to 1.0. Ranking falls back to score order. |
| `no-llm` | The summary stage is skipped. |
| `no-change-correlation` | `RCAParams.w_change_correlation = 0.0`. |

`ABLATION_NAMES` is locked: `("no-correlation", "no-rca", "no-llm", "no-change-correlation")`.
Each is a one-line substitution into `replay_run`; each is unit-tested.

### The D1 invariant — `no-llm` cannot change the ranking

`no-llm` is the interesting one: the LLM is presentation-only (D1), so
**top-1 accuracy must be byte-identical with and without it.** If it is not, a code
path is leaking model output into a decision — that is a bug this ablation exists to
catch. The invariant is asserted in `tests/unit/evaluation/test_ablations.py` on a
hand-built graph + anomalies.

## 10.7 EvalParams — environment

`EvalParams` honours the `INFRAMIND_EVAL_*` prefix (uses `inframind.common.config.load_prefixed`):

| Variable | Default | Meaning |
|---|---|---|
| `INFRAMIND_EVAL_SCENARIO_SET` | `dev` | `dev` or `test`. |
| `INFRAMIND_EVAL_REPEATS` | 5 | D6: ≥5 runs per scenario. |
| `INFRAMIND_EVAL_SEED` | unset | Overrides the scenario's seed; bit-reproducible. |
| `INFRAMIND_EVAL_FIXTURE_DIR` | `evaluation/fixtures/` | NDJSON replay corpus. |
| `INFRAMIND_EVAL_RESULTS_DIR` | `evaluation/results/phase7/` | Where numbers land. |
| `INFRAMIND_EVAL_DETECT_WINDOW_S` | 120.0 | Grace period between fault onset and incident deadline. |
| `INFRAMIND_EVAL_ALLOW_BLOCKED` | `false` | When `false`, a blocked scenario aborts the run. |
| `INFRAMIND_EVAL_ALLOW_OVERWRITE` | `false` | When `false`, refuses to overwrite a results directory that has content. |
| `INFRAMIND_EVAL_SETS` | `("dev", "test")` | The set names resolved by `--set all` (advanced; the CLI owns it). |
| `INFRAMIND_EVAL_RUN_BASELINES` | `true` | Whether the runner reports six baseline rows in addition to `infra`. |
| `INFRAMIND_EVAL_RUN_ABLATIONS` | `true` | Whether `--ablation` produces the four ablation rows. |

## 10.8 Makefile targets

```makefile
eval:            ## Replay the dev set through InfraMind + all baselines
	$(RUN) -m inframind.evaluation.runner.main --set dev

eval-all:        ## Dev + test. The scored-once run.
	$(RUN) -m inframind.evaluation.runner.main --set all

eval-soak:       ## Fault-free window; measures the false-positive rate (D6).
	$(RUN) -m inframind.evaluation.runner.main --set dev --soak

eval-ablation:   ## The four ablations.
	$(RUN) -m inframind.evaluation.runner.main --set dev --ablation
```

All four default to **replay**. `--live` is the flag that reaches for `kubectl`, and no
Make target sets it — a cluster-touching run must be typed by a human.

## 10.9 The `runner.main` CLI

```
inframind.evaluation.runner.main [--set {dev|test|all}] [--dry-run]
                                 [--repeat N] [--seed N]
                                 [--fixture-dir PATH] [--results-dir PATH]
                                 [--soak | --ablation]
                                 [--allow-blocked] [--allow-overwrite]
                                 [--live]
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Run completed; results written. |
| 2 | Result-row validation failed (a row without `run_id` provenance). |
| 3 | A blocked scenario was present and `--allow-blocked` was not set. |
| 4 | The `--live` flag was set outside a `kind-*` kubeconfig context. |

`--dry-run` prints the resolved plan (per-scenario row count, baseline list, ablation list)
and exits 0 without writing anything.

## 10.10 The results directory — the "no hand-written numbers" rule

`report.write_all(rows, results_dir, *, params, allow_overwrite=False)` writes:

- `evaluation/results/phase7/<scenario_set>_<method>.csv` — one row per run.
- `evaluation/results/phase7/summary.json` — aggregates + `generated_at` + the
  `(scenario_id, seed)` provenance of every input row + a `coverage` block + the
  `EvalParams` echo.
- `evaluation/results/phase7/README.md` — states plainly: *"these numbers were produced
  by `make eval`; no number in this directory was written by hand."*

### The `coverage` block — the anti-misread guard

An aggregate is only meaningful against its denominator. `summary.json` therefore carries:

```json
"coverage": {
  "dev": {
    "scenario_count": 2,
    "scenarios": ["dev-podchaos-01-...", "dev-podchaos-03-..."],
    "methods": ["deepest_error_span", "highest_error_service", "infra", "..."],
    "blocked_scenarios": []
  }
}
```

plus a `warnings` list when the corpus is partial or a blocked row contributed. A bare
`top1: 1.000` over 10 rows reads like a perfect result until you notice the corpus was 2
scenarios, not 8 — `coverage` is what stops that being cited as the D6 matrix. **P9's
`results-to-latex` must prefix any aggregate with its `coverage` line.**

`prepare_dir` raises `FileExistsError` on a non-empty directory unless
`INFRAMIND_EVAL_ALLOW_OVERWRITE=1`. An evaluation run is a deliberate act.

**No numbers are committed.** The directory ships with the README and a `.gitkeep`. P9's
rule ("never invent a number") is enforced by the fact that a hand-written CSV would
have no `run_id` provenance and the report test rejects one (see 10.11).

## 10.11 Tests

**Unit** (`tests/unit/evaluation/`):

- `test_scenarios.py` — every YAML parses into a `ScenarioSpec`; malformed specs raise;
  **dev and test `scenario_id` sets are disjoint**; `assert_disjoint(dev, test)` succeeds;
  exactly 6 files carry the blocked marker.
- `test_replay.py` — replay is deterministic (same fixture twice → identical ranking);
  NDJSON ts-ordering is enforced; the seed fixtures replay to top-1.
- `test_scoring.py` — top-1 / top-3 / MRR / latency / FPR on hand-computed fixtures;
  `default_scorer` is the seam.
- `test_baselines.py` — each of the six returns a valid ranking from a fixture; each
  refuses an empty service set.
- `test_ablations.py` — **`no-llm` produces byte-identical ranking** (the D1 invariant).
- `test_report.py` — a results row without `run_id` provenance is rejected; overwrite is
  refused without the flag.
- `test_module_dag.py` — `evaluation` does not import `storage`, `alerting`, or `api`.

**Integration** (`tests/integration/test_evaluation_replay.py`): the full replay path over
the seed corpus with no cluster — this is the real gate. It runs green on this box.

## 10.12 Hard rules

- **D6: ≥12 scenarios × 5 runs + fault-free soak.** Numbers in the paper come only from
  this matrix.
- **D6: dev/test split.** Never tune on test.
- **D7: 6 baselines.** `BASELINE_NAMES` is a frozen tuple. Adding or removing one is a
  D-change.
- **Never invent a number.** Every result is from `evaluation/results/phase7/`. The README
  says so and the report test enforces it.
- **Every scenario has a `seed`.** No scenario without a seed. `seed_override` records
  the override; the row carries the actual seed used.
- **Replays are signal-time-driven.** No `datetime.utcnow()` inside correlation; no wall
  clock inside `replay_run`.

## 10.13 Out of scope for this PR

- **Live cluster runs.** No docker daemon on this box. `live.py` ships with the kind-*
  context check and the blocked-scenario guard wired, but `run_scenario` is
  `NotImplementedError`. The recorder side lands with the first real P7 run.
- **Fixture recording.** A live run will eventually write a `signals.ndjson` for replay;
  that path is `live.py`'s next step. The seed fixtures in this PR are checked-in
  manually.
- **Committing any result numbers.** Nothing to report until a run happens. P9's
  "never invent a number" is honoured by committing an empty directory.
- **LaTeX / `results-to-latex`.** P9. P7 produces JSON and CSV; P9 renders tables.

## 10.14 Cross-reference

- **Skills:** `.claude/skills/run-evaluation/SKILL.md`.
- **Locked decisions:** D6 (matrix), D7 (baselines), **D7-Replay** (signal-time replay is
  the reproducibility mechanism — in `decision-tree.md`), **D2-Chaos-runtime** (chaos
  kind/containerd — in `decision-tree.md`).
- **Correlation contract:** `docs/standards/06c-correlation-contract.md`.
- **Paper writing:** `docs/standards/12-paper-writing.md` — where these numbers land.
- **Results → LaTeX:** `.claude/skills/results-to-latex/SKILL.md`.
