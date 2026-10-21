# 06c. Correlation contract — incidents, fingerprints, dedup, state machine
> standards · Process · InfraMind. Pairs with the `incident-tracker` skill in `.claude/skills/`.
> P7 makes the contract explicit so the evaluation harness can replay against it.

## 06c.1 Scope

This file pins the **correlation stage** as a contract: what an `Incident` is, how its
`fingerprint` is built, how deduplication works, and the state machine. The detector side
is in `05-add-detector.md`; this file is about the unit that detector output feeds into and
that alerting reads from.

```
Signal → Anomaly ──► IncidentTracker ──► Incident (published to bus / recorded by P6)
                       │
                       │  correlation.window_s   group cohesion
                       │  correlation.cooldown_s when to call it resolved
                       │  correlation.max_age_s  hard ceiling
                       └─► BaselineFreezeCoordinator  (D4: graph freezes around the incident)
```

## 06c.2 The `Incident` value

`Incident` is the **only** thing correlation publishes. Everything downstream — alerting,
storage, the evaluation harness — reads from this shape (`src/inframind/common/incident.py`).
Frozen because P7's `ResultRow.run_id` references `Incident.id` and the paper cites the
schema:

| Field | Type | Origin | Meaning |
|---|---|---|---|
| `id` | ULID (26 chars) | `tracker.on_anomaly` | Stable for the lifetime of one opening; **fresh on every re-open**. |
| `revision` | int | snapshot counter | Starts at 1, increments on each publish. |
| `state` | `open` \| `acknowledged` \| `resolved` \| `expired` | state machine | The lifecycle position. |
| `opened_at` | tz-aware UTC datetime | first anomaly's `ts` | The signal time the incident was first seen. |
| `updated_at` | tz-aware UTC datetime | the publishing tick's signal time | Strictly non-decreasing. |
| `closed_at` | tz-aware UTC datetime \| None | the publishing tick that resolved/expired | Non-`None` iff `state` is terminal. |
| `title` | str | derived | `"<first_service>: <first_detector> anomaly (+N services)"`. |
| `services` | `tuple[str, ...]` (sorted) | derived | The frozen set of services in the incident. |
| `signals` | `tuple[str, ...]` | alert signals that corroborated | Source-of-truth for "what evidence backed this". |
| `anomalies` | `tuple[Anomaly, ...]` | the retained window | Capped per detector, then overall (06c.5). |
| `severity` | `Severity` | `_classify` | WARNING by default, CRITICAL under rules in 06c.6. |
| `fingerprint` | 16 hex chars | `correlation.fingerprint` | Stable for the lifetime of an opening (06c.3). |

The contract: `id` is the *opening* identity; `fingerprint` is the *cohesion* identity.
Both are present so P7 can replay the opening and the suppression separately.

## 06c.3 The fingerprint

A fault that flaps inside the same `window_s` bucket must not produce two pages. The
fingerprint is built once at opening and never changes:

```
fingerprint = sha256(normalise_service(first_service) + "|" + bucket).hexdigest()[:16]
where bucket = floor(opened_at.timestamp() / window_s) * window_s
```

- `first_service` is the **opening** service — the service on the anomaly that opened the
  incident. A growing service set never moves the fingerprint.
- `window_s` is the same field the tracker uses for cohesion. The fingerprint is therefore
  comparable within one config and meaningless across configs that change `window_s`.
- `normalise_service` collapses kebab/underscore variants so `cartservice` and
  `cart-service` open with the same fingerprint.

**Why not the ULID?** ULIDs are time-ordered within a millisecond but not bucket-aligned.
Flapping faults need a bucket-aligned key, not a clock-aligned one.

## 06c.4 Joining an existing incident

`choose_incident(active, anomaly.service, anomaly.ts, window_s)` (`correlation/window.py`)
is the cohesion rule. An anomaly joins an *active* (open or acknowledged) record iff:

1. its service is already in `record.service_set`, **or**
2. its `ts` is within `window_s` of the record's `last_anomaly_ts`.

When both conditions are met, multiple records are possible; the chosen one is the one
with the most-recent `last_anomaly_ts`, then the most-recent `opened_at`, then the lowest
`id`. The deterministic tie-break matters because replay runs (P7) depend on it: same
fixture → same joined record → same `revision` history.

If no active record matches, `on_anomaly` calls `_open` and publishes the first snapshot.

## 06c.5 Retention

The tracker keeps the **newest** `max_per_detector=10` anomalies of each detector, then
the newest `max_anomalies=50` overall. The same cap is applied to `signals`
(`max_signals=200`). The cap is per-record, not per-detector: one noisy detector cannot
evict evidence from the others.

`_release` only unfreezes a service when **no other active record** still includes it
— the `BaselineFreezeCoordinator` (D4) sees the per-service union, not the per-record
view.

## 06c.6 Severity classification

`_classify(record)` returns:

- `CRITICAL` if **any** of:
  - the record's current severity is `CRITICAL`,
  - the worst retained anomaly is `CRITICAL`,
  - `len(service_set) >= critical_services` (default 3),
  - `anomaly_count >= critical_anomalies` (default 5).
- `WARNING` otherwise, unless the opening severity was `INFO`, in which case stay `INFO`.

Graph-distance weighting (P5) refines this for ranking, but the **classification** is
deliberately conservative: alerting escalates on the correlation view, not on the graph
view.

## 06c.7 The state machine

`correlation/state_machine.py`:

```
            open ──acknowledge──► acknowledged
              │                        │
              │                        │ (auto-resolve on quiet for cooldown_s,
              │                        │  or expire on max_age_s)
              ▼                        ▼
            resolved ◄────────────────► acknowledged
            (cooldown)                (cooldown max_age)

            either terminal ──► resolved | expired
```

`is_active(state)` is `True` for `open` and `acknowledged`. An anomaly from a service
already in an active record never opens a *new* record — it attaches to the existing one.
A signal from such a service attaches via `attach_signal` and may never publish a new
snapshot on its own.

Terminal states (`resolved`, `expired`) free the frozen services and remove the record
from `_active` in the same `tick`. The terminal-state `closed_at` is the publishing tick.

## 06c.8 Publishing cadence

A snapshot is published on:

- opening,
- a severity *raise* (WARNING → CRITICAL, INFO → WARNING/CRITICAL),
- a **new service** joining,
- an `acknowledge` / `resolve` / `expire` transition,
- otherwise, at most once per `update_interval_s` (default 30 s) when the record is
  dirty (`revision > 0`, evidence changed since the last publish).

Anything not on this list is silent. The cadence is the rate absorber (D6 property):
the bus does not see one event per anomaly, it sees one event per *state change or
interval*. The deterministic cadence is what makes `replay_run` reproducible: the
publish ticks are a function of signal time, not of wall time.

## 06c.9 P7 consequences

- A run is reproducible from `(scenario_id, seed)` **iff** all times flowing through the
  tracker are signal times. The harness asserts this: every anomaly's `ts` is the signal's
  `ts`, every `tick` is the fixture's `ts` for the corresponding tick.
- `run_id` carries the fingerprint idea forward: the harness's `ResultRow.run_id`
  references the incident timeline by `(scenario_id, seed, repeat, method)`.
- The fault-free soak run (`make eval-soak`) measures false-positive rate from the same
  contract: any incident opened during a fault-free window is a false positive, by
  definition.

## 06c.10 Hard rules

- **Never** mutate `record.services` while an incident is active (D4).
- **Never** recompute a fingerprint — if you need a fresh one, re-open the incident.
- **Never** `tick` on wall time inside correlation — use the signal's `ts`.
- **Never** publish an incident whose `id` is a new ULID after `resolve`/`expire` — the
  record is gone.

## 06c.11 Cross-reference

- **Implementation:** `src/inframind/correlation/{tracker,window,fingerprint,state_machine}.py`.
- **Incident value:** `src/inframind/common/incident.py`.
- **P7 harness:** `src/inframind/evaluation/runner/replay.py` — the deterministic replay
  caller that exercises this contract.
