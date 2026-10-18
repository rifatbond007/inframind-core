# P4 Detection — Design

Status: written without a question round (the user opted out of questions and asked to build from
the docs). Every call below is mine, argued from `docs/`, `CLAUDE.md` and measurements of the
real signals already on `stream:signals`; none is something the user approved. Read the
"Doc conflicts and my rulings" table first.

## Intent

Turn the normalized `Signal`s the P3 collectors publish into **anomalies**, and a minimal incident
lifecycle on `stream:incidents`, so that when a fault hits the testbed the system notices within
the proposal's 60 s MTTD target without crying wolf on a quiet cluster. P4 is the first stage that
decides something; P5 (RCA) consumes what it emits.

The phase gate (`.claude/skills/phase-gates/SKILL.md`, P4 → P5) is: Z-score fires on a synthetic
spike; EWMA fires on a synthetic spike; baseline freeze works while an incident is open;
end-to-end anomaly → incident.

## Goals

1. Four per-service statistical detectors with the shared `update / freeze / unfreeze` API of
   standard 05: **Z-score** and **EWMA** (metrics), **error-rate spike** (logs and error spans),
   **p95 latency regression** (traces). D3: statistical only, no learning.
2. **Baseline freeze (D4)**: while a service has an open incident its baselines stop updating, the
   detectors keep scoring against the frozen baseline (so a sustained fault keeps firing), and
   they resume after resolution or after the freeze lease expires.
3. A minimal **incident tracker** that turns anomalies into `Incident`s on `stream:incidents` and
   drives freeze/unfreeze: one incident per service, opened on the first anomaly, resolved after a
   quiet cooldown.
4. A runnable **worker** (`python -m inframind.correlation.worker`) for the "Detection +
   Correlation" stage, a **live gate** (`make smoke-detection`), and docs/agents/skills that match.
5. Deterministic: the same ordered signals produce the same anomalies (D6). Detectors use the
   signal's own `ts`, never the wall clock.

## Non-goals (and anti-goals)

- **No cross-service grouping, fingerprint dedup, or full state machine.** That is P4b
  (`correlation-engineer`). The P4 tracker is deliberately per-service and simple; it must be
  replaceable without touching detectors.
- No persistence (P6 storage): detector baselines and open incidents are in memory and are lost on
  restart, which standard 05 §5.3 already accepts.
- No Alertmanager-signal detection (alerts are already decisions; P4b attaches them to incidents),
  no seasonal models, no per-pod state, no threshold tuning (P7, on the dev split only, D6).
- Anti-goal: a detector that scores well on a spike but silently stops firing under a sustained
  fault, or one that false-alarms on the quiet cluster. Both defeat the purpose.

## Doc conflicts and my rulings

The docs disagree with each other, with the code, and with the data. Each row is a decision I took;
Task 1 of the plan writes them into the standards and the decision log.

| # | Conflict | Ruling | Evidence |
|---|---|---|---|
| 1 | Std 05 §5.4 example returns `None` when frozen; §5.6 says "a frozen detector still emits `Anomaly`" | Frozen detectors **score but do not update** the baseline. §5.4 example is wrong. | D4 text ("does not update the baseline"); §5.6 "which is the point" |
| 2 | `freeze()` has no argument in §5.1, takes `service` in §5.4 | `freeze(service, ts)` / `unfreeze(service)` | per-service state (§5.3) |
| 3 | Z-score window: 60 samples (§5.4) vs 300 s (agent file) | **60 samples**, warm-up `min_samples=10` (≈5 min at the 30 s poll, matching the agent's 5-min baseline) | standard is the canonical procedure |
| 4 | `Anomaly` lives in `detection/anomaly.py` (§5.5) but `rca` may import only `common` and the evidence bundle needs anomalies | Wire models `Anomaly` and `Incident` live in **`common/`**; `detection/anomaly.py` re-exports `Anomaly` so the standard's import path works | standards 01 §1.3 DAG is a hard rule |
| 5 | `std = sqrt(var) or 1e-9` | Per-metric **absolute + relative std floors** | on real quiet CPU history: 13 of 1624 samples (0.8%) false-fire with the 1e-9 floor, 0 with `abs 0.005 cores + 10%` |
| 6 | The example Z-scores `attrs["value"]` of every metric in one window | State keyed **(service, metric)**; counters (`*_total`) are fed as non-negative **deltas** | three metric kinds share a service; `container_restarts_total` is monotonic |
| 7 | Incident states: D4 `OPEN/ACKNOWLEDGED/RESOLVED`, surface-map adds `expired`, correlation agent says `open → updating → resolved` | States `open, acknowledged, resolved, expired` (surface-map). "Updating" is a **`revision` bump** on a republish, not a state. | one vocabulary for P4b/storage |
| 8 | D4: baselines resume after `RESOLVED` "or after the configured decay window"; std 05 §5.6 calls it a safety net | The freeze is a **renewable lease** of `BASELINE_FREEZE_WINDOW` (default 300 s, existing key). The tracker renews it on every anomaly, so a live incident stays frozen; the lease only expires if the tracker vanishes. | satisfies both texts without absorbing a live fault |
| 9 | Gate says "anomaly → incident" but P4b owns correlation | P4 ships a **minimal per-service tracker** (needed anyway: D4 freeze is driven by incident state) | gate wording; D4 |
| 10 | "Error *rate*" detector, but collectors publish only warning/error lines, no totals | **Count-based** spike detector over a sliding window; documented limitation | `ingestion/loki.py` drops non-problem lines by design |
| 11 | Env scheme: std 05 `INFRAMIND_DETECTION_<DETECTOR>_<PARAM>`; surface-map `BASELINE_FREEZE_WINDOW`, `DETECTION_CONCURRENCY` | Detector params use the first scheme; freeze lease uses the existing `BASELINE_FREEZE_WINDOW`; `DETECTION_CONCURRENCY` is not implemented (one worker, YAGNI) | both are documented keys |
| 12 | Time source unspecified | Signal `ts` (observation time) drives windows, decay and cooldown; wall clock is used only to resolve incidents when the stream is idle | D6 reproducibility |

## Architecture

```
stream:signals ──> SignalConsumer (group "correlation") ──> DetectionEngine ──> Anomaly[]
                                                                   ▲                │
                                              freeze(service)/unfreeze(service)      ▼
                                                  BaselineFreezeCoordinator <── IncidentTracker ──> stream:incidents
```

One process, the "Detection + Correlation" worker (`correlation/worker.py`); `correlation` may
import `detection` (standards 01 §1.3). New and changed modules:

| Module | Responsibility |
|---|---|
| `common/anomaly.py` | `Anomaly` (frozen pydantic): `id` ULID, `ts`, `detector`, `service`, `signal_id`, `score`, `severity: Severity`, `attrs`. Std 05 §5.5 plus `id` and `ts` (MTTD and windows need them). |
| `common/incident.py` | `Incident` wire model: `id`, `revision`, `state`, `opened_at`, `updated_at`, `closed_at`, `title`, `services`, `signals` (ids), `anomalies` (newest 50), `severity`. RCA outputs (candidates, summary, alerts) are added downstream, not on this stream. |
| `common/bus.py` | `publish_incident(client, incident)` to `stream:incidents` (pinned). |
| `detection/config.py` | typed params + `from_env()` for `INFRAMIND_DETECTION_*`; metric profiles (floors, counter flag). |
| `detection/base.py` | `Detector` protocol (`update`, `freeze`, `unfreeze`), per-series idempotence guard (skip `ts` ≤ last seen). |
| `detection/zscore.py`, `ewma.py`, `error_spike.py`, `latency_regression.py` | the four detectors. |
| `detection/freeze.py` | `BaselineFreezeCoordinator`: lease per service, fans freeze/unfreeze to all detectors, expires leases by signal time. |
| `detection/engine.py` | `DetectionEngine.process(signal) -> list[Anomaly]`: routes by `type`/`source`/`attrs`. |
| `correlation/tracker.py` | `IncidentTracker`: per-service open/update/resolve, severity, freeze lease renewal. |
| `correlation/worker.py` | consumer loop + idle ticks + `python -m inframind.correlation.worker`. |

## Detector specifications

Defaults come from the standards and the agent file, adjusted by the measurements in the
last section. All are tunable by `INFRAMIND_DETECTION_<DETECTOR>_<PARAM>`.

**Common rules.** State is per series; the series key is `(service, metric)`. A sample whose `ts`
is not newer than the series' last processed `ts` is ignored, which makes at-least-once redelivery
harmless. Until `min_samples` are held, a detector learns and never fires. A frozen series keeps
scoring and does not append to its window.

**Z-score** (`zscore.py`, `Signal.type == METRIC`, `source == PROMETHEUS`).
`window=60` samples, `threshold=3.0`, `min_samples=10`. `mean`/`std` over the window,
`std = max(std, abs_floor, rel_floor * |mean|)`, fire when `|x - mean| / std >= threshold`.
Metric profiles: `cpu_cores` abs 0.005 / rel 0.10; `memory_working_set_bytes` abs 8 MiB / rel
0.05; `container_restarts_total` is a counter (delta, abs floor 0.25, so a single restart scores
4.0); unknown metrics rel 0.10, abs 0. `score` is the signed z.

**EWMA** (`ewma.py`, metrics). `alpha=0.3`, `sigma_k=3`, `min_samples=10`, same floors. Exponentially
weighted mean and variance; fire when `|x - mean| > sigma_k * max(sqrt(var), floors)`. Catches
slow ramps the windowed Z-score absorbs. `score` is the residual in sigmas.

**Error-rate spike** (`error_spike.py`). Counts *error events* per service in a sliding window of
`window_s=300`: Loki `LOG` signals (already filtered to warning/error) and Jaeger error spans
(`TRACE` signals carrying `span_id`). Baseline = mean events per window over the last
`baseline_windows` sliding evaluations while unfrozen. Fire when
`count >= max(min_count, k * baseline)` with `min_count=3`, `k=3`. Because the collectors send no
totals, this is a count spike, not a ratio (ruling 10). `score` = count / max(baseline, 1).

**Latency regression** (`latency_regression.py`, Jaeger `span_latency` signals). Skip polls with
`span_count < min_spans=10`. Baseline = rolling median of the last `window=60` p95 values. A poll
*breaches* when `p95 > median * (1 + regression_pct/100)` and `p95 - median >= min_delta_ms=10`
(`regression_pct=50`). Fire only after `consecutive=2` breaching polls. `score` = p95 / median.

**Severity** (`Severity` from `common`): Z-score/EWMA `warning` below 1.5x the threshold,
`critical` at or above; error spike `critical` when `count >= 2*min_count` else `warning`;
latency `critical` when `p95 >= 3x median`. Counter restarts are `critical`.

## Freeze lifecycle (D4)

1. First anomaly for a service with no open incident: the tracker opens an incident and calls
   `coordinator.freeze(service, ts)`; every detector marks that service's series frozen.
2. Each further anomaly for that service renews the lease (`freeze` again) and updates the
   incident. Frozen detectors keep scoring against the frozen baseline.
3. After `cooldown_s=120` of signal-time without an anomaly for the service the tracker resolves
   the incident and calls `unfreeze(service)`. Wall-clock ticks resolve incidents when the stream
   is idle.
4. Safety net: if no renewal arrives for `BASELINE_FREEZE_WINDOW` (300 s), the coordinator expires
   the lease on the next signal for that service.

## Incident tracker (P4 minimal)

Per service: at most one open incident. Publish on **open**, on **severity increase**, and on
**resolve**; coalesce other updates (the in-memory incident still grows). `revision` increments on
every publish. `title` is `"<service>: <detector> anomaly"`. P4b replaces windowing, fingerprinting
and cross-service grouping; the tracker's interface (`on_anomaly`, `tick`) is what P4b keeps.

## Testing and verification

- Unit tests per detector with synthetic series: fires on the spike with the exact score, quiet on
  the baseline, quiet on a flat series (floors), counter delta, idempotent redelivery, freeze
  keeps scoring and does not learn, unfreeze resumes learning, lease expiry by signal time.
- Replay test: feed the recorded real signals (a small committed fixture, < 5 MB) and assert the
  false-alarm count on the quiet portion is 0 for metrics.
- Tracker tests: open/update/resolve, severity-increase republish, freeze/unfreeze calls, idle tick.
- Worker test with `fakeredis`: signals in, incident out on `stream:incidents`.
- **Live gate** `make smoke-detection`: collectors at 5 s + worker; a quiet baseline produces no
  incident; a burst of parallel requests to the frontend raises its CPU, an incident opens within
  60 s, the baselines freeze, and after the burst the incident resolves and unfreezes.
- Standing gates: `make lint`, `make test`, `pre-commit run --all-files`, `make smoke-testbed`,
  `make smoke-ingestion`.

## Constraints

Python 3.11+, no new dependencies, ruff/mypy clean, module DAG (`detection` imports only
`common`; `correlation` may import `detection`), commits on `dev-rifat` without AI trailers (D25),
docs and agents updated in the same change as the standards (phase-gates sync rule).

## What the data says (measurements behind the defaults)

From 6244 real signals already on `stream:signals`:
- CPU of the busiest services is 0.003 to 0.013 cores with std 0.002 to 0.008; the standard's 1e-9
  std floor false-fires on 0.8% of quiet samples, `abs 0.005 + 10%` on 0%.
- The frontend p95 is bimodal and bursty (median 16 ms, p90 75 ms, max 5 s). A single-poll rule
  fires on 16% of its polls even with robust thresholds; requiring 2 consecutive breaching polls
  cuts that to 2%, 3 to 0%. `consecutive=2` is the compromise with the 60 s MTTD target at a 30 s
  poll interval; P7 may retune on the dev split.
- Polls with fewer than 10 spans (checkout, payment under no load) are noise: median 2 spans.
- Error events are rare (56 in the whole history), so a count rule with `min_count=3` is quiet at
  baseline.

## Left to the builder

Exact class layouts, the Anomaly `attrs` keys per detector (document them in the docstring), log
field names, the fixture format for the replay test, and the burst-traffic mechanics of the live
gate. If the live burst cannot lift frontend CPU above the floors on this host, use whatever
load actually does (more parallel clients, or scaling `loadgenerator` for the test) and say so.
