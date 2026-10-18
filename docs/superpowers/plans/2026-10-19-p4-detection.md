# P4 Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the P3 `Signal` stream into per-service anomalies and a minimal incident lifecycle on `stream:incidents`, with baseline freeze (D4), a runnable worker, a live gate, and docs/agents/skills that match.

**Architecture:** Four statistical detectors (Z-score, EWMA, error-count spike, p95 latency regression) share one `update/freeze/unfreeze` API and are driven by a `DetectionEngine`. A `BaselineFreezeCoordinator` holds a renewable freeze lease per service. A per-service `IncidentTracker` turns anomalies into `Incident`s, drives the freeze, and a `CorrelationWorker` wires `stream:signals` → engine → tracker → `stream:incidents`. Wire models live in `common/` because `rca` may import only `common`.

**Tech Stack:** Python 3.11+, pydantic v2, redis-py asyncio, pytest + fakeredis, ruff, mypy. **No new dependencies.**

**Spec:** `docs/superpowers/specs/2026-10-19-p4-detection-design.md` (read its "Doc conflicts and my rulings" table first; every ruling there is binding for this plan). Sources it argues from: `docs/standards/05-add-detector.md`, `docs/decision-tree.md` (D3, D4, D6), `.claude/skills/phase-gates/SKILL.md` (P4 → P5).

## Global Constraints

- Python `>=3.11`; ruff line length 100, rules `E,F,W,I,B,UP,SIM,RUF`; `mypy src` clean; pytest `--strict-markers`, `asyncio_mode=auto`.
- **D3:** statistical only, no learning. **D4:** baselines freeze while an incident is open; frozen detectors still score. **D6:** same ordered signals produce the same anomalies, so detectors use `Signal.ts`, never the wall clock; tune on dev split only (no tuning in this plan).
- Module DAG (standards 01 §1.3, enforced by `tests/unit/test_module_dag.py`): `detection` imports only `common`; `correlation` may import `common` and `detection`; `rca` imports only `common`.
- State is **per service** and keyed `(service, metric)` for metrics; in memory; lost on restart (standard 05 §5.3).
- Stream names are pinned: `stream:signals`, `stream:incidents`, `stream:graph-updates`.
- Tunables via `INFRAMIND_DETECTION_<DETECTOR>_<PARAM>` (upper case); the freeze lease via the existing `BASELINE_FREEZE_WINDOW` (default `300`); cooldown via `INFRAMIND_CORRELATION_COOLDOWN_S` (default `120`).
- Branch `dev-rifat`; Conventional Commits; author `user.name` is the local part of a `@users.noreply.github.com` email; **no AI `Co-Authored-By:` trailer** (D25).
- Gates after every task: `ruff check src tests testbed`, `mypy src`, `pytest -q -m ""` (the scratch venv `$V` until the repo `.venv` is repaired). Clear `__pycache__` after any mutation experiment.
- The pinned pre-commit ruff is authoritative; if the commit hook reformats files, re-stage and recommit (check `git log` shows a new commit before recording a task).
- Never `--no-verify`, never force-push, never push to `main`.

## Review Focus

1. **Idle or flat series**: a zero-variance series that wiggles by a tiny amount must not fire (floors) — Task 3 test `test_flat_series_with_a_tiny_wiggle_does_not_fire`.
2. **Counter reset**: `container_restarts_total` dropping when a pod is replaced must not produce a negative delta or an anomaly — Task 3 test `test_counter_reset_is_not_an_anomaly`.
3. **Redelivered or out-of-order signals**: the same `ts` twice must neither double-fire nor double-learn — Tasks 3–6 `test_redelivered_signal_is_ignored`.
4. **Sustained fault**: an incident longer than the freeze lease stays open, stays frozen while anomalies keep arriving, and is never absorbed into the baseline — Tasks 7 and 9.
5. **Idle stream and replays**: an incident resolves on wall-clock ticks when the stream is quiet, but replaying old signals must not be resolved prematurely by the wall clock — Task 10.

---

### Task 1: Wire models `Anomaly` and `Incident`, and `publish_incident`

**Files:**
- Create: `src/inframind/common/anomaly.py`, `src/inframind/common/incident.py`
- Modify: `src/inframind/common/bus.py`
- Test: `tests/unit/common/test_anomaly.py`, `tests/unit/common/test_incident.py`, additions to `tests/unit/common/test_graph_update.py`'s sibling `tests/unit/common/test_bus_incident.py`

**Interfaces:**
- Produces:
  ```python
  class Anomaly(BaseModel):  # frozen, extra="forbid"
      id: str            # default new_ulid
      ts: datetime       # aware UTC, observation time of the source signal
      detector: str      # "zscore" | "ewma" | "error_spike" | "latency_regression"
      service: str       # canonical lower-kebab, "unknown" rejected
      signal_id: str
      score: float       # finite
      severity: Severity
      attrs: dict[str, Any]  # JSON-serialisable; non-JSON keys dropped like Signal.attrs

  IncidentState = Literal["open", "acknowledged", "resolved", "expired"]
  class Incident(BaseModel):  # frozen, extra="forbid"
      id: str; revision: int  # >= 1
      state: IncidentState
      opened_at: datetime; updated_at: datetime; closed_at: datetime | None
      title: str; services: list[str]  # non-empty, sorted, canonical
      signals: list[str]; anomalies: list[Anomaly]; severity: Severity
  async def publish_incident(client: Redis, incident: Incident, *, stream: str = stream_keys.INCIDENTS, maxlen: int = DEFAULT_MAXLEN) -> str
  ```
  Validation: `closed_at` must be set iff `state` is `resolved` or `expired`; naive or epoch timestamps rejected as in `Signal`/`GraphUpdate`.

- [ ] **Step 1: Write the failing tests.** `test_anomaly.py`: round-trips through JSON; service canonicalised (`"FrontEnd"` → `"frontend"`); `"unknown"` and empty rejected; naive `ts` and epoch `ts` rejected; `score=float("nan")` and `inf` rejected; non-JSON attr value dropped; frozen; extra field rejected. `test_incident.py`: round-trips with two anomalies; `closed_at` required for `resolved`, forbidden for `open`; empty `services` rejected; `revision=0` rejected; services sorted and de-duplicated. `test_bus_incident.py`: `publish_incident` writes one entry whose field `data` parses back to an equal `Incident` on `stream:incidents` and nothing on `stream:signals`; a `ConnectionError` raises `BusError`.
- [ ] **Step 2: Run** `pytest tests/unit/common -q`. Expected: FAIL (modules missing).
- [ ] **Step 3: Implement** the two models and `publish_incident`, mirroring `common/graph_update.py` and `publish_graph_update` (same validators, same `data` field, same `BusError` wrapping). Reuse `normalise_service`/`UNKNOWN_SERVICE` and `new_ulid`.
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(common): add Anomaly and Incident wire models and publish_incident`.

---

### Task 2: Detection config, metric profiles and shared building blocks

**Files:**
- Create: `src/inframind/detection/config.py`, `src/inframind/detection/base.py`
- Test: `tests/unit/detection/__init__.py`, `tests/unit/detection/test_config.py`, `tests/unit/detection/test_base.py`

**Interfaces:**
- Produces (`config.py`):
  ```python
  @dataclass(frozen=True) class MetricProfile: abs_floor: float; rel_floor: float; counter: bool = False
  def profile_for(metric: str) -> MetricProfile
  # cpu_cores -> (0.005, 0.10); memory_working_set_bytes -> (8*2**20, 0.05);
  # container_restarts_total -> (0.25, 0.0, counter=True); anything else -> (0.0, 0.10)
  @dataclass(frozen=True) class ZScoreParams: window: int = 60; threshold: float = 3.0; min_samples: int = 10
  @dataclass(frozen=True) class EwmaParams: alpha: float = 0.3; sigma_k: float = 3.0; min_samples: int = 10
  @dataclass(frozen=True) class ErrorSpikeParams: window_s: int = 300; min_count: int = 3; k: float = 3.0; baseline_windows: int = 12
  @dataclass(frozen=True) class LatencyParams: window: int = 60; regression_pct: float = 50.0; min_delta_ms: float = 10.0; min_spans: int = 10; consecutive: int = 2; min_samples: int = 10
  def load_params(cls: type[T], detector: str, env: Mapping[str, str] | None = None) -> T
  def freeze_window_s(env: Mapping[str, str] | None = None) -> float   # BASELINE_FREEZE_WINDOW, default 300
  def cooldown_s(env: Mapping[str, str] | None = None) -> float        # INFRAMIND_CORRELATION_COOLDOWN_S, default 120
  ```
  `load_params` reads `INFRAMIND_DETECTION_<DETECTOR>_<FIELD>` (upper case), casts to the field's type, and raises `ValueError` naming the variable on a bad value; `env=None` means `os.environ`.
- Produces (`base.py`):
  ```python
  class Detector(Protocol):
      name: str
      def update(self, signal: Signal) -> Anomaly | None
      def freeze(self, service: str, ts: datetime) -> None
      def unfreeze(self, service: str) -> None
      def is_frozen(self, service: str) -> bool
  class FrozenServices: freeze(service); unfreeze(service); is_frozen(service) -> bool
  class LastSeen:  def is_new(self, key: Hashable, ts: datetime) -> bool   # True and remembers when ts > last for key
  ```

- [ ] **Step 1: Write the failing tests.** `test_config.py`: `profile_for("cpu_cores") == MetricProfile(0.005, 0.10)`; restarts profile is a counter with floor 0.25; unknown metric → `MetricProfile(0.0, 0.10)`; `load_params(ZScoreParams, "zscore", {})` equals defaults; `{"INFRAMIND_DETECTION_ZSCORE_THRESHOLD": "4.5"}` → `threshold == 4.5` and `window == 60`; `{"INFRAMIND_DETECTION_ZSCORE_WINDOW": "abc"}` raises `ValueError` whose message contains `INFRAMIND_DETECTION_ZSCORE_WINDOW`; `freeze_window_s({})` is `300.0`, `{"BASELINE_FREEZE_WINDOW": "60"}` is `60.0`; `cooldown_s({})` is `120.0`. `test_base.py`: `LastSeen.is_new` is True for a first ts, False for the same or an older ts, True for a newer one, independently per key; `FrozenServices` toggles per service.
- [ ] **Step 2: Run** `pytest tests/unit/detection -q`. Expected: FAIL.
- [ ] **Step 3: Implement** both modules (dataclass field types drive the casting; stdlib only).
- [ ] **Step 4: Run** the full gates. Expected: PASS (also `tests/unit/test_module_dag.py`: `detection` imports only `common`).
- [ ] **Step 5: Commit** `feat(detection): add detector config, metric profiles and shared building blocks`.

---

### Task 3: `ZScoreDetector`

**Files:**
- Create: `src/inframind/detection/zscore.py`
- Test: `tests/unit/detection/test_zscore.py`

**Interfaces:**
- Consumes: `Anomaly`, `Signal`, `Severity` (common); `ZScoreParams`, `profile_for` (Task 2); `Detector`, `FrozenServices`, `LastSeen` (Task 2).
- Produces: `class ZScoreDetector: name = "zscore"; def __init__(self, params: ZScoreParams | None = None)`, implementing `Detector`.
  Behaviour: considers only `Signal.type is METRIC` with `source is PROMETHEUS` and a numeric `attrs["value"]`; series key `(service, attrs["metric"])`; counters (`profile.counter`) are fed as `max(0, value - previous)` (the first sample only stores `previous`, a decrease is a reset and yields delta 0); the window holds the last `params.window` values; nothing fires until `min_samples` are held; `std = max(population_std, profile.abs_floor, profile.rel_floor * abs(mean))`; fires when `abs(x - mean)/std >= threshold`; `score` = signed z; severity `CRITICAL` when `abs(z) >= 1.5 * threshold` or the metric is a counter, else `WARNING`; `attrs` = `{"metric", "value", "mean", "std", "window", "threshold", "frozen"}`; baseline (window) is updated **after** scoring and only when the service is not frozen; the scoring of a frozen series still happens.

- [ ] **Step 1: Write the failing tests** (helper `metric(service, metric, value, ts)` building a `Signal(source=PROMETHEUS, type=METRIC, attrs={"metric":..., "value":...})`, 30-second spacing):
  - `test_spike_after_warmup_fires`: 20 samples alternating `0.010`/`0.014`, then `0.2` → `Anomaly` with `detector == "zscore"`, `service`, `signal_id == spike.id`, `score > 3`, `severity is CRITICAL`, `attrs["frozen"] is False`, and `attrs["mean"]` ≈ `0.012`.
  - `test_quiet_series_never_fires`: 60 samples alternating `0.010`/`0.014` → all `None`.
  - `test_warmup_never_fires`: first 9 samples `[0.01]*8 + [10.0]` → all `None`.
  - `test_flat_series_with_a_tiny_wiggle_does_not_fire` (Review Focus 1): 20 samples of `0.0` then `0.004` → `None` (abs floor 0.005).
  - `test_warning_vs_critical_severity`: window alternating `0.0`/`0.02` (mean 0.01, std 0.01): `0.045` → `WARNING` (z=3.5); `0.06` → `CRITICAL` (z=5.0).
  - `test_counter_delta_fires_on_first_restart`: 15 samples of `container_restarts_total=0`, then `1` → fires, `score == 4.0`, `severity is CRITICAL`.
  - `test_counter_reset_is_not_an_anomaly` (Review Focus 2): totals `0,0,…,3,3,…,0,0,0` (drop from 3 to 0) → the drop yields `None` and later zeros stay `None`.
  - `test_redelivered_signal_is_ignored` (Review Focus 3): feed the spike signal twice → only the first returns an `Anomaly`; the window length in a following anomaly's `attrs["window"]` is not inflated.
  - `test_metrics_of_one_service_are_independent`: a CPU spike does not change the memory series' `attrs["mean"]`.
  - `test_non_metric_and_non_prometheus_signals_are_ignored`: a `LOG` signal and a `JAEGER` metric-like signal return `None`.
  - `test_frozen_series_keeps_scoring_but_does_not_learn`: after warm-up call `freeze("frontend", ts)`, feed 30 samples of `0.2` → each returns an `Anomaly` with `attrs["frozen"] is True`; the last one's `attrs["mean"] < 0.02`. After `unfreeze("frontend")` five more `0.2` samples raise `attrs["mean"]` above `0.03` (learning resumed).
- [ ] **Step 2: Run** `pytest tests/unit/detection/test_zscore.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `ZScoreDetector` per the contract above (deque window per series; population std).
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(detection): add the Z-score detector with std floors and counter deltas`.

---

### Task 4: `EwmaDetector`

**Files:**
- Create: `src/inframind/detection/ewma.py`
- Test: `tests/unit/detection/test_ewma.py`

**Interfaces:**
- Consumes: as Task 3, with `EwmaParams`.
- Produces: `class EwmaDetector: name = "ewma"; def __init__(self, params: EwmaParams | None = None)`. Same signal selection, series key, counter handling, floors, idempotence and freeze semantics as Task 3. State per series: `mean`, `var`, `n`. First sample sets `mean = x`, `var = 0`. Once `n >= min_samples`: `sd = max(sqrt(var), abs_floor, rel_floor*abs(mean))`, `residual = x - mean`, fire when `abs(residual) > sigma_k * sd`; `score = residual / sd`; severity `CRITICAL` when `abs(score) >= 1.5 * sigma_k` or counter, else `WARNING`; `attrs = {"metric","value","mean","std","alpha","sigma_k","frozen"}`. After scoring, when not frozen: `mean += alpha*residual; var = (1-alpha) * (var + alpha*residual**2)`.

- [ ] **Step 1: Write the failing tests:** `test_step_change_fires` (20 samples alternating `0.010`/`0.014`, then `0.2` → fires, `score > 3`); `test_quiet_series_never_fires` (60 alternating samples); `test_warmup_never_fires`; `test_flat_series_with_a_tiny_wiggle_does_not_fire`; `test_counter_delta_fires_on_first_restart` and `test_counter_reset_is_not_an_anomaly`; `test_redelivered_signal_is_ignored`; `test_frozen_series_keeps_scoring_but_does_not_learn` (30 frozen samples at `0.2` all fire, final `attrs["mean"] < 0.02`; after `unfreeze`, `mean` moves toward `0.2` within five samples, `> 0.1`); `test_it_is_not_a_slow_ramp_detector`: values rising `0.012 → 0.05` over 25 samples never fire (documents the design ruling that EWMA follows level shifts).
- [ ] **Step 2: Run** `pytest tests/unit/detection/test_ewma.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `EwmaDetector` (share the counter-delta and floor helpers from Task 3 by moving them into `detection/base.py` if both files need them; do not duplicate).
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(detection): add the EWMA detector`.

---

### Task 5: `ErrorSpikeDetector`

**Files:**
- Create: `src/inframind/detection/error_spike.py`
- Test: `tests/unit/detection/test_error_spike.py`

**Interfaces:**
- Consumes: as Task 3, with `ErrorSpikeParams`.
- Produces: `class ErrorSpikeDetector: name = "error_spike"; def __init__(self, params: ErrorSpikeParams | None = None)`. An *error event* is a `Signal` with `source is LOKI and type is LOG`, or `source is JAEGER and type is TRACE and "span_id" in attrs`. Per service keep a deque of `(ts, counted_in_baseline)` retained for `(baseline_windows + 1) * window_s`; a seen-signal-id set (bounded) makes redelivery harmless. On each event: `count` = events with `ts` in `(ts - window_s, ts]`; `baseline` = events flagged `counted_in_baseline` with `ts` in `(ts - (baseline_windows+1)*window_s, ts - window_s]` divided by `baseline_windows`; the new event is flagged `counted_in_baseline = not frozen`. Fire when `count >= max(min_count, k * baseline)`; `score = count / max(baseline, 1.0)`; severity `CRITICAL` when `count >= 2 * min_count` else `WARNING`; `attrs = {"count","baseline","window_s","min_count","k","frozen","source"}`.

- [ ] **Step 1: Write the failing tests** (`error(service, source, ts)` helper; Loki errors carry `level`, Jaeger errors carry `span_id`):
  - `test_third_error_in_a_window_fires`: events at 0, 20, 40 s → `None, None, Anomaly`; `score == 3.0`, `severity is WARNING`; six events within 60 s → the sixth is `CRITICAL`.
  - `test_errors_spread_over_the_window_do_not_fire`: events at 0, 200, 400 s → all `None`.
  - `test_jaeger_error_spans_and_loki_errors_count_together`: two Loki + one Jaeger error within a window fires on the third.
  - `test_other_signals_are_ignored`: Jaeger `span_latency` signals, Prometheus metrics, and Alertmanager signals return `None`.
  - `test_a_noisy_baseline_raises_the_bar`: one error every 150 s for 3900 s (about 2 per window) never fires; a burst of 8 errors within 10 s afterwards fires, and the first burst events that bring `count` below `max(3, 3*baseline)` do not.
  - `test_events_during_a_freeze_do_not_enter_the_baseline`: `freeze`, then 40 errors spread over 1000 s (some fire), `unfreeze`, 400 s of quiet, then three errors in 40 s → fires with `attrs["baseline"] == 0.0`.
  - `test_redelivered_signal_is_ignored` (Review Focus 3): the same signal id twice counts once.
  - `test_separate_services_have_separate_counts`.
- [ ] **Step 2: Run** `pytest tests/unit/detection/test_error_spike.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `ErrorSpikeDetector` per the contract (prune the deque by retention horizon on every event).
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(detection): add the error-count spike detector`.

---

### Task 6: `LatencyRegressionDetector`

**Files:**
- Create: `src/inframind/detection/latency_regression.py`
- Test: `tests/unit/detection/test_latency_regression.py`

**Interfaces:**
- Consumes: as Task 3, with `LatencyParams`.
- Produces: `class LatencyRegressionDetector: name = "latency_regression"; def __init__(self, params: LatencyParams | None = None)`. Considers `Signal`s with `source is JAEGER`, `type is TRACE`, `attrs["metric"] == "span_latency"`. Skip (return `None`, learn nothing) when `attrs["span_count"] < min_spans`. Per service: a deque of the last `window` p95 values (`attrs["p95_ms"]`) and a breach run length. With `>= min_samples` held, `median` of the history; a poll *breaches* when `p95 > median * (1 + regression_pct/100)` and `p95 - median >= min_delta_ms`; run length increments on a breach, resets otherwise; fire when run length `>= consecutive`; `score = p95 / median` (guard `median` with a minimum of `0.001`); severity `CRITICAL` when `p95 >= 3 * median`, else `WARNING`; `attrs = {"p95_ms","median_ms","span_count","consecutive","regression_pct","frozen"}`. History is appended after scoring and only when not frozen; a frozen series keeps scoring and keeps its run length. `ts` idempotence via `LastSeen`.

- [ ] **Step 1: Write the failing tests** (`latency(service, p95, spans, ts)` helper):
  - `test_two_consecutive_breaches_fire`: 15 polls at `16 ms` (`span_count=40`), then `60`, `60` → the first `60` returns `None`, the second returns an `Anomaly` with `score ≈ 3.75`, `severity is CRITICAL`.
  - `test_a_single_isolated_spike_does_not_fire`: `16 … 16, 77, 16 …` never fires (the real frontend pattern).
  - `test_polls_with_few_spans_are_ignored_and_not_learned`: `span_count=2` polls at `5000 ms` return `None` and do not change the median in a later anomaly's `attrs["median_ms"]`.
  - `test_small_absolute_regressions_do_not_fire`: baseline `0.07 ms` rising to `0.2 ms` (`+0.13 ms < min_delta_ms`) never fires even when `2.8x`.
  - `test_warmup_never_fires`.
  - `test_redelivered_signal_is_ignored` (Review Focus 3).
  - `test_error_span_signals_and_other_metrics_are_ignored`.
  - `test_frozen_series_keeps_scoring_but_does_not_learn`: after freezing, 10 polls at `60 ms` all fire from the second on, `attrs["median_ms"]` stays `16.0`; after `unfreeze` the median moves.
- [ ] **Step 2: Run** `pytest tests/unit/detection/test_latency_regression.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `LatencyRegressionDetector` per the contract (`statistics.median`).
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(detection): add the p95 latency regression detector`.

---

### Task 7: `BaselineFreezeCoordinator`

**Files:**
- Create: `src/inframind/detection/freeze.py`
- Test: `tests/unit/detection/test_freeze.py`

**Interfaces:**
- Consumes: `Detector` (Task 2).
- Produces:
  ```python
  class BaselineFreezeCoordinator:
      def __init__(self, detectors: Sequence[Detector], lease_s: float = 300.0) -> None
      def freeze(self, service: str, ts: datetime) -> None     # (re)sets lease to ts + lease_s; calls detector.freeze on every detector
      def unfreeze(self, service: str) -> None                 # drops the lease; calls detector.unfreeze on every detector; no-op if not frozen
      def expire(self, now: datetime) -> set[str]              # unfreezes and returns services whose lease ended (lease <= now)
      def is_frozen(self, service: str) -> bool
  ```

- [ ] **Step 1: Write the failing tests** with two recording fake detectors: `freeze` fans out `freeze(service, ts)` to both; `expire(t + 299 s)` returns `set()` and `expire(t + 301 s)` returns `{service}` and calls `unfreeze` on both exactly once; a second `freeze(service, t + 200 s)` renews the lease so `expire(t + 301 s)` is empty and `expire(t + 501 s)` returns it (**sustained fault, Review Focus 4**); `unfreeze` before expiry clears the lease and later `expire` returns `set()`; `unfreeze` of an unfrozen service calls no detector; services are independent; `lease_s` honoured.
- [ ] **Step 2: Run** `pytest tests/unit/detection/test_freeze.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with a `dict[str, datetime]` of lease deadlines.
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(detection): add the baseline freeze coordinator with a renewable lease`.

---

### Task 8: `DetectionEngine`

**Files:**
- Create: `src/inframind/detection/engine.py`, `src/inframind/detection/anomaly.py` (re-export), edit `src/inframind/detection/__init__.py`
- Test: `tests/unit/detection/test_engine.py`

**Interfaces:**
- Consumes: the four detectors, `BaselineFreezeCoordinator`, `load_params`, `freeze_window_s`.
- Produces:
  ```python
  class DetectionEngine:
      def __init__(self, detectors: Sequence[Detector], coordinator: BaselineFreezeCoordinator) -> None
      @classmethod
      def default(cls, env: Mapping[str, str] | None = None) -> "DetectionEngine"   # all four detectors with env-loaded params; coordinator lease = freeze_window_s(env)
      @property coordinator -> BaselineFreezeCoordinator
      def process(self, signal: Signal) -> list[Anomaly]    # first coordinator.expire(signal.ts), then every detector's update, in a fixed order
  ```
  `detection/anomaly.py` does `from inframind.common.anomaly import Anomaly` and `__all__ = ["Anomaly"]` so standard 05's import path works.

- [ ] **Step 1: Write the failing tests:** `test_prometheus_metric_spike_returns_zscore_and_ewma_anomalies` (the same spike yields both detectors' anomalies, order `zscore` then `ewma`); `test_span_latency_signals_reach_only_the_latency_detector`; `test_error_logs_reach_only_the_error_spike_detector`; `test_alertmanager_signals_return_nothing`; `test_default_honours_env_overrides` (`INFRAMIND_DETECTION_ZSCORE_THRESHOLD=100` stops a spike that fires by default; `BASELINE_FREEZE_WINDOW=60` shortens the lease); `test_expired_leases_are_released_before_scoring` (freeze a service via the coordinator at `t`, process a signal at `t + 301 s` → the detectors report the service unfrozen); `test_the_standard_import_path_works` (`from inframind.detection.anomaly import Anomaly` is the `common` class). Gate tests named after the phase gate: `test_gate_zscore_fires_on_a_synthetic_spike`, `test_gate_ewma_fires_on_a_synthetic_spike`, `test_gate_baseline_freeze_keeps_a_sustained_fault_firing` (60 frozen samples at the fault level all return anomalies and the baseline `mean` is unchanged).
- [ ] **Step 2: Run** `pytest tests/unit/detection/test_engine.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `DetectionEngine`, the re-export module, and docstring/`__all__` in `detection/__init__.py`.
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(detection): add the detection engine and the standard import path`.

---

### Task 9: `IncidentTracker`

**Files:**
- Create: `src/inframind/correlation/tracker.py`
- Test: `tests/unit/correlation/__init__.py`, `tests/unit/correlation/test_tracker.py`

**Interfaces:**
- Consumes: `Anomaly`, `Incident`, `Severity` (common); `BaselineFreezeCoordinator` (Task 7).
- Produces:
  ```python
  class IncidentTracker:
      def __init__(self, coordinator: BaselineFreezeCoordinator, *, cooldown_s: float = 120.0,
                   max_anomalies: int = 50, max_signals: int = 200) -> None
      def on_anomaly(self, anomaly: Anomaly) -> list[Incident]   # [] or [incident] to publish
      def tick(self, now: datetime) -> list[Incident]             # resolved incidents to publish
      @property open_services -> frozenset[str]
  ```
  Rules: no open incident for `anomaly.service` → create `Incident(state="open", revision=1, title=f"{service}: {detector} anomaly", services=[service], severity=anomaly.severity, opened_at=updated_at=anomaly.ts)`, call `coordinator.freeze(service, anomaly.ts)`, return it. Open incident → append the anomaly (keep the newest `max_anomalies`) and its `signal_id` (newest `max_signals`), `updated_at = anomaly.ts`, renew `coordinator.freeze(service, anomaly.ts)`; if `anomaly.severity` outranks the incident's (`INFO < WARNING < CRITICAL`) raise it, bump `revision` and return the snapshot; otherwise return `[]`. `tick(now)`: for each open incident with `now - last_anomaly_ts >= cooldown_s` → snapshot with `state="resolved"`, `closed_at=now`, `revision + 1`; call `coordinator.unfreeze(service)`; drop it; return the snapshots in service order.

- [ ] **Step 1: Write the failing tests** (real `BaselineFreezeCoordinator` over a recording fake detector): `test_first_anomaly_opens_one_incident_and_freezes`; `test_further_anomalies_update_without_republishing` (returns `[]`, but a later resolve snapshot contains all anomalies); `test_a_severity_increase_republishes_with_a_new_revision`; `test_quiet_cooldown_resolves_and_unfreezes` (`tick(last + 119 s) == []`, `tick(last + 120 s)` returns one `resolved` incident with `closed_at` set and the detector's `unfreeze` called); `test_anomalies_renew_the_freeze_lease` (**Review Focus 4**: anomalies every 100 s for 1000 s with `lease_s=300` keep the service frozen at every step); `test_services_are_independent`; `test_a_service_can_reopen_after_resolving` (new id, `revision == 1`); `test_caps_keep_the_newest_anomalies_and_signal_ids`; `test_open_services_reflects_state`.
- [ ] **Step 2: Run** `pytest tests/unit/correlation -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `IncidentTracker` (mutable internal records; publish immutable `Incident` snapshots).
- [ ] **Step 4: Run** the full gates. Expected: PASS (DAG test: `correlation` imports `common` and `detection` only).
- [ ] **Step 5: Commit** `feat(correlation): add the minimal per-service incident tracker`.

---

### Task 10: `CorrelationWorker` and its entrypoint

**Files:**
- Create: `src/inframind/correlation/worker.py`
- Test: `tests/unit/correlation/test_worker.py`

**Interfaces:**
- Consumes: `SignalConsumer`, `publish_incident`, `DetectionEngine`, `IncidentTracker`, `cooldown_s`.
- Produces:
  ```python
  class CorrelationWorker:
      def __init__(self, redis: Redis, engine: DetectionEngine, tracker: IncidentTracker, *,
                   group: str = "correlation", consumer: str = "worker-1",
                   idle_tick_s: float = 5.0, live_window_s: float = 600.0,
                   clock: Callable[[], datetime] = utcnow, in_stream: str = stream_keys.SIGNALS,
                   out_stream: str = stream_keys.INCIDENTS) -> None
      async def handle(self, signal: Signal) -> None     # engine.process -> tracker.on_anomaly -> publish; advances the watermark (max signal ts) and ticks the tracker at the watermark
      async def tick_idle(self) -> None                   # tracker.tick(clock()) only when the watermark is within live_window_s of clock()
      async def run(self, stop: asyncio.Event) -> None
  async def amain() -> None    # python -m inframind.correlation.worker; env: REDIS_URL, thresholds via DetectionEngine.default(), INFRAMIND_CORRELATION_COOLDOWN_S
  ```

- [ ] **Step 1: Write the failing tests** (`fakeredis`, `SignalConsumer.poll_once(worker.handle)`): `test_signals_in_incident_out` (warm-up metrics then a spike published with `bus.publish` → one `Incident` appears on `stream:incidents`, `state == "open"`, `services == ["frontend"]`); `test_quiet_signals_publish_nothing`; `test_resolution_by_signal_time`: after the spike, a later signal for any service with `ts` past the cooldown → the resolve snapshot appears; `test_idle_tick_resolves_when_the_stream_is_quiet`: with an injected `clock` past the cooldown and a watermark inside `live_window_s` → `tick_idle` publishes the resolve (**Review Focus 5**); `test_idle_tick_does_not_resolve_a_replay`: signals stamped an hour before `clock()` leave the incident open after `tick_idle`; `test_handler_errors_do_not_lose_the_signal` (a publish failure raises so `SignalConsumer` keeps the entry pending); `test_redelivery_does_not_open_a_second_incident` (the same spike signal handled twice).
- [ ] **Step 2: Run** `pytest tests/unit/correlation/test_worker.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the worker; `amain` mirrors `ingestion/runner.py`'s signal handling and always closes Redis; `if __name__ == "__main__": asyncio.run(amain())`.
- [ ] **Step 4: Run** the full gates, then start it for 5 s against the compose Redis (`python -m inframind.correlation.worker`, SIGINT) and confirm a clean exit with a `worker_started` log line.
- [ ] **Step 5: Commit** `feat(correlation): add the correlation worker that turns signals into incidents`.

---

### Task 11: Replay regression test on real recorded signals

**Files:**
- Create: `tests/fixtures/quiet_cluster_signals.ndjson` (under 5 MB), `tests/fixtures/README.md`, `tests/unit/detection/test_replay.py`

**Interfaces:**
- Consumes: `DetectionEngine.default`.

- [ ] **Step 1: Extract the fixture** from the compose Redis with one documented command (`XRANGE stream:signals - +`): keep Prometheus `cpu_cores` and `memory_working_set_bytes` signals only, and choose the longest contiguous stretch (at least 20 minutes) in which no `container_restarts_total` signal increases; write `tests/fixtures/README.md` with the command, the selected time range, and the statement that this is a regression guard on floors, not an accuracy claim.
- [ ] **Step 2: Write the failing test.** `test_quiet_cluster_produces_no_metric_anomalies`: replay the fixture in `ts` order through `DetectionEngine.default({})` and assert `anomalies == []`. `test_an_injected_spike_in_real_data_is_found`: multiply one `cpu_cores` sample of `frontend` after warm-up by 50 → exactly that signal produces a `zscore` anomaly. `test_replay_is_deterministic`: two replays give identical `(detector, signal_id, round(score, 9))` lists.
- [ ] **Step 3: Run** `pytest tests/unit/detection/test_replay.py -q`. Expected: the quiet test PASSES if the floors are right; if it FAILS, that is a real defect in a floor or the warm-up: fix `config.py` (and say so in the commit), not the fixture.
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `test(detection): add a replay regression guard on real recorded signals`.

---

### Task 12: Live gate `make smoke-detection`

**Files:**
- Create: `testbed/smoke_detection.py`, `tests/unit/test_smoke_detection.py`
- Modify: `Makefile`, `testbed/README.md`

**Interfaces:**
- Produces (pure, importable by the test): `evaluate(incidents: list[Incident], burst_started: datetime, burst_ended: datetime, cooldown_s: float) -> list[CheckResult]` returning, in order, checks `quiet`, `detected`, `frozen`, `resolved` (same `CheckResult` shape as `testbed/smoke_ingestion.py`; reuse it by import, do not copy):
  - `quiet`: no incident has `opened_at < burst_started`.
  - `detected`: an incident for the burst target opened within 60 s of `burst_started` (the proposal's MTTD).
  - `frozen`: the resolved incident holds at least two anomalies, every anomaly after the first has `attrs["frozen"] is True`, and their `attrs["mean"]` equals the first anomaly's (the baseline did not move).
  - `resolved`: a `resolved` snapshot of that incident exists with `closed_at` within `burst_ended + cooldown_s + 30 s`.

- [ ] **Step 1: Write the failing tests** for `evaluate` with hand-built incidents: a healthy run passes all four; an incident opened before the burst fails `quiet`; opening 61 s after the burst fails `detected`; a later anomaly with `frozen=False` or a drifted `mean` fails `frozen`; no resolve fails `resolved`.
- [ ] **Step 2: Run** `pytest tests/unit/test_smoke_detection.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `evaluate` and the live driver: start the three collectors at 5 s and the worker (`INFRAMIND_DETECTION_ZSCORE_MIN_SAMPLES=10`, `INFRAMIND_CORRELATION_COOLDOWN_S=45`, `BASELINE_FREEZE_WINDOW=300`) writing to the real pinned streams, port-forward the frontend, drive light baseline traffic for the warm-up and a 60 s quiet check, then a burst of parallel requests for 40 s, then stop traffic and wait for the resolve; read `stream:incidents` after a recorded id; always clean up subprocesses (as `smoke_ingestion.py`). The builder chooses the burst mechanics (parallel `httpx` clients through the port-forward); if the frontend CPU does not rise above its floor, use heavier load and record what worked in the commit message. Add Makefile target `smoke-detection` and a README table.
- [ ] **Step 4: Run** `make smoke-detection PY=$V` against the live testbed. Expected: 4/4 checks pass in under 6 minutes, no processes left. Then prove it can fail by running with the worker disabled via `SMOKE_TIMEOUT_S=1`: exit 1 with failing checks named.
- [ ] **Step 5: Commit** `feat(testbed): add smoke-detection, the live P4 gate`.

---

### Task 13: Align standards, agents, skills, decision log and progress with the code

Per the `phase-gates` sync rule, the standard and the agents that own the code ship together.

**Files:**
- Modify: `docs/standards/05-add-detector.md` (rewrite §5.1–5.9 to the real API: `freeze(service, ts)`, scoring while frozen, floors, `(service, metric)` keys, counters, the four detectors' parameters, `Anomaly` in `common`, the freeze lease, `INFRAMIND_DETECTION_*`, the real tests and `make smoke-detection`), `docs/surface-map.md` (add the `Anomaly` and wire `Incident` models, `INFRAMIND_DETECTION_*`, `INFRAMIND_CORRELATION_COOLDOWN_S`, note `DETECTION_CONCURRENCY` is not implemented), `docs/decision-tree.md` (one change-log entry listing rulings 1–12 of the spec), `.claude/agents/build/detection-engineer.md` and `.claude/agents/build/correlation-engineer.md` (parameters, files, states, "updating" is a revision bump, P4 tracker vs P4b), `.claude/skills/add-detector/SKILL.md`, `src/inframind/detection/README.md`, `src/inframind/detection/__init__.py`, `src/inframind/correlation/README.md`, `src/inframind/correlation/__init__.py`, `src/inframind/common/README.md`, `CLAUDE.md` (commands: worker, `make smoke-detection`; architecture bullet), `docs/PROGRESS.md` (P4 row, phase checklist, current phase), `docs/standards/10-evaluation.md` (MTTD = injection time to the first `Anomaly.ts`).
- Test: extend `tests/unit/test_repo_metadata.py`.

- [ ] **Step 1: Write the failing tests.** Add to `REMOVED_NAMES`: `r"or 1e-9"`, `r"detection\.severity"`, `r"severity_from_zscore"`. `test_standard_05_matches_the_detector_api`: the standard names `freeze(service`, `ts` , `BASELINE_FREEZE_WINDOW`, `INFRAMIND_DETECTION_`, `common/anomaly.py`, and no longer contains `return None  # D4`. `test_detection_agent_lists_only_existing_detectors`: every `*.py` in the detection-engineer collector table exists under `src/inframind/detection/`. `test_surface_map_documents_the_new_wire_models`: contains `class Anomaly` and `revision`.
- [ ] **Step 2: Run** `pytest tests/unit/test_repo_metadata.py -q`. Expected: FAIL listing every stale reference (this list is the work).
- [ ] **Step 3: Edit each file** until the tests pass; change only what is wrong, keep rationale that is still true.
- [ ] **Step 4: Run every gate:** `pre-commit run --all-files`, `make lint`, `make test`, `make test-all`, `make smoke-testbed` (6/6), `make smoke-ingestion` (5/5), `make smoke-detection` (4/4). Record each result in the PROGRESS row; walk the P4 → P5 checklist line by line (`Z-score fires`, `EWMA fires`, `baseline freeze works`, `anomaly → incident`) with the test or run that proves it.
- [ ] **Step 5: Commit** `docs(detection): align standards, agents and skills with the P4 detection code`.

---

## Self-review

- **Spec coverage:** goals 1–5 → Tasks 3–6, 7+9, 9+10, 12, determinism via `ts` in Tasks 2–6; rulings 1–12 → Tasks 3 (1,5,6), 2 (2,11), 1 (4,7), 7+9 (8), 9 (9), 5 (10), 10 (12), all recorded in Task 13; replay evidence → Task 11; live MTTD → Task 12.
- **Type consistency:** `Detector.freeze(service, ts)` (Task 2) is what the coordinator calls (Task 7) and the tracker triggers (Task 9); `Anomaly`/`Incident` field names (Task 1) are the ones Tasks 3–6, 9, 10 and 12 construct and read; `load_params`/`freeze_window_s`/`cooldown_s` (Task 2) are used in Tasks 8 and 10.
- **Ordering:** 1 → 2 → 3–6 (independent, share Task 2) → 7 → 8 → 9 → 10 → 11 → 12 → 13; Task 13 last because it asserts the final state of all docs.
- **Proportion:** the plan lists signatures, parameter values and test assertions; bodies are left to the implementer except where the algorithm is not determined by them.
- **Open items after this plan:** P4b (cross-service windows, fingerprint dedup, full state machine), persistence of baselines and incidents (P6), threshold tuning on the dev split (P7), and the two chaos blockers from the hardening round, which gate any P7 scenario that needs daemon-based faults.
