# 5. Add a detector — Z-score / EWMA / error spike / latency regression
> standards · Python conventions · InfraMind. Pairs with the `add-detector` skill in `.claude/skills/add-detector/SKILL.md`. This file is the rationale and the worked Z-score example. The design behind it, with the evidence for every default, is `docs/superpowers/specs/2026-10-19-p4-detection-design.md`.

## 5.1 The shape every detector has

```
src/inframind/detection/<detector>.py
```

```python
from inframind.common.anomaly import Anomaly
from inframind.common.signal import Signal


class <Name>Detector:
    """Statistical <method> detector. Stateful per service."""

    name = "<detector>"

    def __init__(self, params: <Name>Params | None = None) -> None: ...
    def update(self, signal: Signal) -> Anomaly | None: ...
    def freeze(self, service: str, ts: datetime) -> None: ...
    def unfreeze(self, service: str) -> None: ...
    def is_frozen(self, service: str) -> bool: ...
```

A detector is a long-lived object that owns per-service state: rolling windows, EWMA
coefficients, or whatever the algorithm needs. The protocol is `detection/base.py:Detector`; the
four implementations are `zscore.py`, `ewma.py`, `error_spike.py` and `latency_regression.py`,
run together by `DetectionEngine` (`engine.py`), which is the only entry point the correlation
worker calls.

## 5.2 Three methods, three responsibilities

- **`update(signal) -> Anomaly | None`** — incremental. Called for every signal; each detector
  ignores the signals it does not model. Returns `None` most of the time and an `Anomaly` when it
  fires.
- **`freeze(service, ts)`** — called when an incident opens on a service (D4). The detector
  stops *learning* that service's baseline. It **keeps scoring** against the frozen baseline.
- **`unfreeze(service)`** — called when the incident resolves. Learning resumes.

The freeze/unfreeze lifecycle is what makes the system correct under a sustained fault. Without
it the fault slowly becomes "normal" and the detector silently stops firing. A test pins that
failure (`test_unfrozen_detectors_absorb_a_sustained_fault`) so nobody mistakes it for a feature.

## 5.3 The state discipline

- **State is per service**, and for metrics per `(service, metric)`: one service has CPU, memory
  and restart series with different noise floors, and a shared window would let one metric
  silence another. A global window is wrong for the same reason across services.
- **State is in-memory.** A restart loses the rolling window. That is acceptable: the next window
  fills in `interval * min_samples` seconds (about 5 minutes at the 30 s poll).
- **State is private.** No module outside `detection/` touches a detector's state.
- **Time is the signal's own `ts`**, never the wall clock, so replaying recorded signals gives
  the same anomalies as the live run (D6).
- **A redelivered signal is a no-op.** The bus is at-least-once; every detector remembers the
  newest `ts` it processed per series (or the signal id, for event counters) so a duplicate
  neither double-fires nor double-learns.
- **Noise floors.** A flat series has a standard deviation near zero, so any wiggle would score
  enormous. The divisor is `max(std, abs_floor, rel_floor * |mean|)` with a per-metric profile in
  `detection/config.py`. On the recorded quiet CPU history, the bare near-zero fallback that earlier
  drafts of this standard used false-fires on 0.8% of samples; the profile gives 0%.
- **Direction.** Resource gauges (`cpu_cores`, `memory_working_set_bytes`) and counters alert
  upward only; a service going idle when traffic stops is not a fault. Unprofiled metrics are
  two-sided.
- **Counters** (`*_total`) are scored on their per-sample increase; a decrease is a reset (the
  pod was replaced) and counts as zero.

## 5.4 Worked example: the Z-score detector

The real code is `src/inframind/detection/zscore.py`; this is its core.

```python
def update(self, signal: Signal) -> Anomaly | None:
    point = metric_point(signal)               # (metric, value) for a Prometheus metric, else None
    if point is None:
        return None
    metric, value = point
    key = (signal.service, metric)
    if not self._seen.is_new(key, signal.ts):  # redelivery guard
        return None
    profile = profile_for(metric)
    if profile.counter:                        # score the increase, not the running total
        delta = self._counters.delta(key, value)
        if delta is None:
            return None
        value = delta
    window = self._windows.setdefault(key, deque(maxlen=self._params.window))
    anomaly = self._score(signal, metric, value, window)   # always scored, even when frozen
    if not self._frozen.is_frozen(signal.service):         # D4: a frozen baseline does not learn
        window.append(value)
    return anomaly
```

`_score` needs `min_samples` held, computes `z = (x - mean) / effective_std(...)`, returns `None`
below `threshold` (or for a downward move on an `"up"` metric) and otherwise an `Anomaly` whose
`attrs` carry `metric`, `value`, `mean`, `std`, `window`, `threshold` and `frozen`.

## 5.5 The four detectors and their defaults

| Detector | Signal | Algorithm | Defaults (`INFRAMIND_DETECTION_<NAME>_<PARAM>`) |
|---|---|---|---|
| `zscore` | Prometheus metric | rolling Z-score per `(service, metric)` | `window=60` samples, `threshold=3.0`, `min_samples=10` |
| `ewma` | Prometheus metric | exponentially weighted mean and variance | `alpha=0.3`, `sigma_k=3.0`, `min_samples=10` |
| `error_spike` | Loki warning/error lines, Jaeger error spans | event count in a sliding window against a rolling baseline | `window_s=300`, `min_count=3`, `k=3.0`, `baseline_windows=12` |
| `latency_regression` | Jaeger `span_latency` polls | p95 against the rolling median, breach must persist | `regression_pct=50`, `min_delta_ms=10`, `min_spans=10`, `consecutive=2`, `window=60` |

- **EWMA vs Z-score.** EWMA has no window edge and follows recent level shifts; with `alpha=0.3`
  its memory is about three samples, so it is not a slow-ramp detector (a test says so).
- **Error spike is a count, not a ratio.** The collectors publish only problem lines and never
  totals, so there is no denominator. Events seen while a service is frozen stay out of the
  baseline, so a long fault cannot raise its own bar.
- **Latency persistence.** Real Boutique frontend p95 is bursty (median 16 ms, isolated spikes
  of 75 ms to 5 s). On recorded data a single breaching poll fires on 16% of polls, two in a row
  on 2%. Polls with fewer than `min_spans` spans are noise and are neither scored nor learned.
- **Severity** is coarse and set by each detector: Z-score/EWMA `critical` at 1.5x the threshold
  (always for a counter), error spike `critical` at twice `min_count`, latency `critical` at 3x
  the median. Correlation refines it.

## 5.6 The `Anomaly` shape

```python
class Anomaly(BaseModel):      # src/inframind/common/anomaly.py, frozen, extra="forbid"
    id: str                    # ULID
    ts: datetime               # observation time of the source signal (tz-aware UTC)
    detector: str              # "zscore" | "ewma" | "error_spike" | "latency_regression"
    service: str               # canonical lower-kebab
    signal_id: str             # back-reference to the source Signal
    score: float               # raw detector score (signed z, residual in sigmas, count/baseline, ratio)
    severity: Severity         # info | warning | critical
    attrs: dict[str, Any]      # detector-specific context, always including "frozen"
```

It lives in `common/anomaly.py`, not in `detection/`, because the RCA stage may import only
`common` and receives anomalies inside `Incident`s on `stream:incidents` (standards 01 §1.3).
`detection/anomaly.py` re-exports it so `from inframind.detection.anomaly import Anomaly` works.
`id` and `ts` are additions to the earlier draft: MTTD and windowing need the observation time.

## 5.7 The freeze discipline (D4)

- The incident tracker calls `coordinator.freeze(service, ts)` when it opens an incident and
  **renews** it on every further anomaly. `BaselineFreezeCoordinator` (`freeze.py`) fans the call
  out to every detector.
- The freeze is a **lease** of `BASELINE_FREEZE_WINDOW` seconds (default 300). While anomalies
  keep arriving the lease keeps renewing, so a live incident stays frozen for as long as it fires.
  If nothing renews it (the tracker went away), the coordinator releases it by signal time on the
  next signal; that is the safety net D4 describes as the "decay window".
- `unfreeze(service)` is called when the incident resolves after `cooldown_s` of quiet.
- A frozen detector **keeps scoring** and so keeps emitting `Anomaly` objects; it just stops
  learning. A sustained fault therefore keeps firing, which is the point.
- Known limitation: the sample that triggers the first anomaly is scored before the freeze
  applies, so it is learned once. The effect is one sample in a 60-sample window.

## 5.8 Tuning protocol

- **Dev split is for tuning.** Thresholds are chosen on `evaluation/scenarios/dev/`. The
  optimisation target is the F1 / MTTD trade-off the team agrees on.
- **Test split is for the report.** Numbers in the paper come from `evaluation/scenarios/test/`
  only. Tuning on the test set is a paper-disqualifying error.
- The defaults above are starting values grounded in recorded signals, not tuned results. The
  split is locked at the start of P7 and never re-shuffled.

## 5.9 Tests

```
tests/unit/detection/test_<detector>.py       # synthetic series with a known spike
tests/unit/detection/test_replay.py           # recorded real signals, quiet and spiked
tests/unit/correlation/test_tracker.py        # incident lifecycle and the freeze it drives
tests/unit/correlation/test_worker.py         # signals in, incidents out
make smoke-detection                          # the live P4 gate
```

- Each unit test feeds a synthetic series with a known spike and asserts the detector fires with
  the expected score, stays quiet on the baseline and on a flat series, ignores a redelivered
  signal, and keeps scoring without learning while frozen.
- `test_replay.py` replays 3516 real CPU/memory samples (`tests/fixtures/README.md`) and asserts
  no metric anomalies; it fails if the Z-score goes two-sided again or the std floor is lost.
  It is a regression guard, not an accuracy claim.
- `make smoke-detection` runs the collectors and the worker against the live testbed, applies a
  burst of load to the frontend, and checks that an incident opens within 60 s, the later
  anomalies are scored against one constant frozen baseline, and the incident resolves.
- Fault injection for the evaluation uses Chaos Mesh; daemon-based faults need the runtime fix
  proposed in `docs/decision-tree.md` (`D2-Chaos-runtime`).

## 5.10 Hard rules

- **State is per service** (per `(service, metric)` for metrics). No global rolling windows.
- **Freeze on incident open, unfreeze on resolve; keep scoring while frozen.** D4. A detector that
  ignores freeze, or that stops firing under a freeze, is broken even if it scores well on dev.
- **Tune on dev, report on test.** D6. The two splits are sacred.
- **No DL in the detector.** D3. Statistical only.
- **Tunable parameters via env vars** `INFRAMIND_DETECTION_<DETECTOR>_<PARAM>` (upper case),
  loaded by `detection/config.py:load_params`; a bad value raises `ValueError` naming the
  variable. The freeze lease is `BASELINE_FREEZE_WINDOW`; the incident cooldown is
  `INFRAMIND_CORRELATION_COOLDOWN_S`.
- **Use the signal's `ts`, never `datetime.utcnow()`.**

## 5.11 The incident tracker and the worker (P4 minimal)

`correlation/tracker.py` (`IncidentTracker`) turns anomalies into `Incident`s: one open incident
per service, opened by the first anomaly (which freezes the service), updated by later ones, and
resolved after `cooldown_s` of signal-time quiet (which unfreezes it). It publishes on open, on a
severity increase and on resolve; `revision` counts publishes. `correlation/worker.py` consumes
`stream:signals`, runs `DetectionEngine` and the tracker, and publishes to `stream:incidents`
(`python -m inframind.correlation.worker`). Cross-service windows, fingerprint deduplication and
the full state machine belong to P4b: one burst on the frontend lifted CPU on six services and
opened six incidents, which P4b will group.

## 5.12 Cross-reference

- **Skill:** `.claude/skills/add-detector/SKILL.md`.
- **Locked decisions:** D3 (no DL), D4 (baseline freeze), D6 (dev/test split). Rulings on where
  this standard changed: `docs/decision-tree.md`, entry `P4-detection-rulings`.
- **Next stage:** `correlation/` (P4b) groups incidents across services.
- **Evaluation:** `10-evaluation.md` — MTTD is the time from fault injection to the first
  `Anomaly.ts`.
