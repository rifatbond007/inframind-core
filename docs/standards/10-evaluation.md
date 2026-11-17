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
    ├── live.py         records fixtures on kind-*: inject, revert, read the bus (10.2)
    └── main.py         CLI: --set, --dry-run, --soak, --ablation, --allow-blocked

evaluation/
├── scenarios/
│   ├── dev/            10 YAMLs (6 runnable + 4 blocked on D2-Chaos-runtime)
│   └── test/           8 YAMLs (6 runnable + 2 blocked on D2-Chaos-runtime)
├── fixtures/<scenario>/r<repeat>/   live recordings only (written by `make eval-live`)
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

`runner/live.py` is the cluster path. A live run **records; it never scores**: scoring
always replays the recording, so every number is reproducible from the fixture alone.
`LiveRunner.run_scenario(spec, repeat=k, record_to=...)`:

1. Asserts the kubeconfig context starts with `kind-` (`$KUBECONFIG` or
   `testbed/.kube/config`, never `~/.kube/config`) and pins `--context` on every call.
   No flag lifts this.
2. Refuses a blocked scenario unless `allow_blocked`, and an existing fixture unless
   `overwrite`.
3. Waits until `stream:signals` holds `max(warmup_s, INFRAMIND_EVAL_BASELINE_S)` seconds
   of history (default 600 s: the detectors need 10 samples, Prometheus is polled every
   30 s).
4. At `onset_ts`, applies the CR from `build_chaos_manifest` (selector →
   `labelSelectors` in `target_namespace`, extra `chaos.spec` keys verbatim, the
   scenario's `duration` so Chaos Mesh self-recovers) in `INFRAMIND_EVAL_CHAOS_NAMESPACE`
   (`chaos-testing`), or the `config_change` (`kubectl set env`).
5. Holds `duration_s`, reverts in a `finally` (delete the CR / `kubectl rollout undo`),
   then waits `cooldown_s + INFRAMIND_EVAL_SETTLE_S` (45 s).
6. Writes `signals.ndjson.gz` (signals observed from `onset - baseline` to the end,
   sorted by `ts`, gzip with no name or time in the header so equal data gives equal bytes;
   about 45 KiB per repeat instead of 600 KiB), `graph.json` (every `GraphUpdate` up to the end folded into a fresh
   `ServiceGraph`), and `meta.json` (`recorded_live: true`, `baseline_s`).

Each D6 repeat is its own recording in `r<repeat>/`; `fixture_dir_for(spec, params,
repeat)` is the only path builder. A missing repeat is logged (`fixture_missing`) and
produces no row, so the report's coverage shows the gap instead of reusing `r0`.
Hand-written fixtures for tests live in `tests/fixtures/eval/` and are never evaluated.

**Which incidents count, and which recordings are scored.** `replay_trace` judges each
incident by its latest snapshot: one that resolved before the fault is noise
(`noise_incidents`), never a detection, so a pre-fault blip cannot become a 0 s latency.
`baseline_problem` refuses a live recording only when an incident is still open at the
fault (it would absorb the fault) or more than `PRE_FAULT_ANOMALY_LIMIT` (20) anomalies
precede it (an earlier fault). Clean baselines show 0-6 anomalies per 10 min; an earlier
fault inside one showed 60-308. A config change is confirmed only when the updated replicas
are the only ones left (`rollout_state`): a new pod that never becomes Ready leaves the old
one serving and the fault never reaches traffic.

## 10.3 Scenarios — 18 YAMLs across the sacred split

`evaluation/scenarios/dev/` (10 files, tuning) and `evaluation/scenarios/test/` (8 files,
scored once). Each YAML:

```yaml
scenario_id: dev-podchaos-01-cartservice-pod-kill
set: dev
seed: 1001
fault_type: pod_kill
target_service: cartservice
target_namespace: default
duration_s: 180
warmup_s: 60
cooldown_s: 60
expected_root_cause: cartservice
expected_affected_services: [cartservice, checkoutservice, frontend]
chaos:
  kind: PodChaos
  spec: {action: pod-kill, mode: one, selector: {label: app, value: cartservice}}
metrics_to_assert: [container_restarts_total, cpu_cores, span_latency]
repeats: 5
```

Service names, the namespace and `expected_affected_services` are Online Boutique's
(`testbed/boutique/`): the affected set is the target plus every service that reaches it
through calls, taken from the manifests' `<callee>:<port>` env vars.
`test_scenarios_match_the_online_boutique_testbed` derives that call graph and fails on any
mismatch. `metrics_to_assert` uses the logical metric names the collectors emit
(`cpu_cores`, `memory_working_set_bytes`, `container_restarts_total`, and the Jaeger
collector's `span_latency`).

Every key of `chaos.spec` other than `action`/`mode`/`selector` (`containerNames`,
`stressors`, `delay`, `direction`) is kept in `ChaosSpec.params` for the live runner. The
loader rejects a fault Chaos Mesh cannot apply (`scenarios.CHAOS_ACTIONS`): PodChaos takes
`pod-kill`/`pod-failure`/`container-kill` (the last needs `containerNames`), StressChaos
takes no `action` and needs `stressors`, and the selector must name `target_service`.

`chaos` may be omitted only when a `config_change` block describes the fault instead (the
test-06 Deployment edit); a scenario with neither is a load error. `ScenarioSpec.is_blocked` returns `True` when `status: blocked`
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
| dev-01 | pod-kill, cartservice | PodChaos | runnable |
| dev-02 | container-kill, shippingservice | PodChaos | runnable (containerd runtime, verified live) |
| dev-03 | pod-kill, productcatalogservice | PodChaos | runnable |
| dev-04 | cpu-stress, cartservice | StressChaos | blocked — D2-Chaos-runtime |
| dev-05 | memory-stress, paymentservice | StressChaos | blocked — D2-Chaos-runtime |
| dev-06 | network delay, emailservice | NetworkChaos | blocked — D2-Chaos-runtime |
| dev-07 | network partition, cartservice | NetworkChaos | blocked — D2-Chaos-runtime |
| dev-08 | pod-kill, recommendationservice | PodChaos | runnable |
| dev-09 | pod-failure, currencyservice | PodChaos | runnable |
| dev-10 | `EXTRA_LATENCY=1s` rollout, productcatalogservice | none (`config_change`) | runnable |
| dev-11 | bad `PRODUCT_CATALOG_SERVICE_ADDR` rollout, frontend (caller bug) | none (`config_change`) | runnable |
| test-01 | pod-kill, checkoutservice | PodChaos | runnable |
| test-02 | container-kill, cartservice | PodChaos | blocked — D2-Chaos-runtime (daemon) |
| test-03 | pod-failure, paymentservice | PodChaos | runnable |
| test-04 | cpu-stress, currencyservice | StressChaos | blocked — D2-Chaos-runtime |
| test-05 | network delay, checkoutservice | NetworkChaos | blocked — D2-Chaos-runtime |
| test-06 | bad `REDIS_ADDR` rollout, cartservice | none (`config_change`) | runnable |
| test-07 | container-kill, emailservice | PodChaos | runnable |
| test-08 | bad `PRODUCT_CATALOG_SERVICE_ADDR` rollout, recommendationservice | none (`config_change`) | runnable |
| test-09 | pod-failure, productcatalogservice | PodChaos | runnable |

No `(fault_type, target_service)` pair appears in both splits
(`test_no_fault_appears_in_both_splits`). **13 of the 20 are runnable** (7 dev + 6 test). Since
D2-Chaos-runtime (containerd runtime, 2026-11-16) `container-kill` injects (verified live) and
dev-02 is runnable again; test-02 and the six Stress/NetworkChaos scenarios stay blocked until
each is probed live. dev-11 (a bad frontend rollout, the live D30 caller-bug check) and test-09
(`pod-failure` on productcatalogservice) were added while container-kill was refused.

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
| 1 | `raw_alertmanager` | Ranks the application services Alertmanager alerted on in the incident window, most alerts first. No alert: it ranks nothing (a miss). | The "what do we already have" floor. |
| 2 | `random_service` | A seeded random order of the service set; the seed is per run (`seed*1000 + repeat`). | The chance floor. Proves top-1 > chance. |
| 3 | `highest_error_service` | Ranks services by error events (Loki problem lines + Jaeger error spans) in the incident window; services with none are not ranked. | The obvious heuristic an SRE writes first. |
| 4 | `deepest_error_span` | Follows each erroring Jaeger span to its target (a failing client span points at the gRPC service it called, e.g. `hipstershop.CartService/GetCart` -> `cartservice`) and ranks by call-graph depth, then error count. | The trace-only heuristic; RCA's closest competitor. |
| 5 | `pagerank` | Runs the P5 `rank_pagerank` on the replayed incident: same symptoms, anomalies, graph and window InfraMind ranked. No incident: ranks nothing. | Proves the graph term earns its keep. |
| 6 | `llm_only` | The LLM names a root cause from evidence alone. **Not run yet:** replay calls no LLM, so the row is skipped and logged (`baseline_not_run`) until LLM answers are recorded per run. | D1's control: the LLM alone must score near chance. If it scores high, that is a finding about the evaluation, not a reason to change the code. |

Every baseline ranks from the same `ReplayTrace` as InfraMind (`replay.replay_trace`): the
same detected incident, its window (`first opened_at` to last update, never the ground-truth
fault window) and its anomalies, each counted once. Evidence-based baselines never append an
alphabetical tail of services they have no evidence for: an earlier version fell back to
alphabetical order, so four baselines all answered `adservice` and scored a free top-3 when
the root cause sorted second. With no incident, baselines read the whole recording.

`BASELINE_NAMES` is the locked tuple. `Baseline` is a `Protocol` with one method:
`rank(baseline_input, summary_text='') -> list[str]`. `BaselineInput` is frozen; an empty
service set raises `EmptyServiceSet`.

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

All four replay. `make eval-live [EVAL_SET=dev|test|all] [SCENARIO=<id>]` is the only
target that reaches for `kubectl`; it records fixtures and is run by a human against
`kind-inframind` after `make up`.

## 10.9 The `runner.main` CLI

```
inframind.evaluation.runner.main [--set {dev|test|all}] [--dry-run]
                                 [--repeat N] [--seed N]
                                 [--fixture-dir PATH] [--results-dir PATH]
                                 [--soak | --ablation]
                                 [--allow-blocked] [--allow-overwrite]
                                 [--live] [--scenario ID ...]
```

`--live` records the missing `r<repeat>` fixtures of every selected runnable scenario and
skips blocked ones (unless `--allow-blocked`); it writes no results. `--scenario` (repeatable)
narrows any run to the named scenario ids.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Run completed; results written. |
| 2 | Result-row validation failed (a row without `run_id` provenance). |
| 3 | A blocked scenario was present and `--allow-blocked` was not set. |
| 4 | The results directory exists and `--allow-overwrite` was not set. |
| 5 | `--live`: not a `kind-*` context, no kubectl, or a recording failed. |

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
the synthetic corpus in `tests/fixtures/eval/` with no cluster. `test_live.py` covers the
recorder with fakeredis, a fake kubectl and a fake clock (CR shape, revert on interrupt,
baseline wait, paging, `--live` CLI).

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

- **Recordings.** The recorder is implemented and unit-tested; no real fixture is
  committed until a human runs `make eval-live` on the testbed.
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
