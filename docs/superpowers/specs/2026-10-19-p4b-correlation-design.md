# P4b Correlation — Design

Status: written without a question round (the user asked to continue to the next phase step by step
from the docs). Every call below is mine, argued from `docs/`, the proposal and what the live P4
gate showed; none is something the user approved.

## Intent

P4's `IncidentTracker` is deliberately minimal: one incident per service. The live P4 gate showed
what that costs: **one burst on the frontend opened six incidents** (every service whose CPU rose),
and a 978-event error flood needed a patch to avoid hiding other evidence. P4b makes the
"Detection + Correlation" stage do what the proposal (§3.2.2) and the correlation agent describe:
group related anomalies into **one** incident with a stable identity, track a real lifecycle, and
absorb the per-event anomaly rate, so RCA receives a single, growing incident per underlying fault.

## Goals

1. **Time-window grouping across services.** Anomalies that belong together form one incident.
2. **A stable fingerprint** for downstream deduplication (alerting, storage).
3. **The full lifecycle**: `open → acknowledged → resolved | expired`, with the D4 freeze held for
   every service in an open incident.
4. **Rate absorption**: publish on meaningful change plus a throttled evidence update, never per
   anomaly.
5. **A `no-correlation` switch** for the D7 ablation ("every anomaly is its own incident").
6. Severity from what the correlation stage can see, and alert signals attached as corroboration.

## Non-goals

- Dependency-graph distance in severity: the graph is owned by the RCA worker (D21) and `correlation`
  may not import `rca`; P5 can refine severity with the graph.
- Merging two already-open incidents; persistence (P6); the acknowledge API route (P6; only the
  state-machine method lands now); tuning (P7, dev split only, D6).
- Anti-goal: a grouping rule so loose that unrelated faults merge silently, or so tight that one
  fault splits across services again.

## Doc conflicts and my rulings

| # | Conflict | Ruling |
|---|---|---|
| 1 | Proposal: group signals that "share the same service **or** fall within a configurable window (default 5 minutes)". Agent: "grouping by service + time". | An anomaly joins an open incident if its service is already in it, otherwise if its `ts` is within `window_s` (300) of that incident's last anomaly; same-service wins, then the most recently active. No merging of existing incidents. |
| 2 | Agent: "fingerprint = hash of (services sorted, 5-min window start). Stable across updates." A services set that grows cannot be stable. | The fingerprint is fixed at open from the **opening** service and the window bucket (`sha256("<first service>|<bucket start>")[:16]`) and never changes while the incident lives. A fault that flaps and re-opens inside the same bucket keeps the fingerprint, so downstream suppression works. The id stays a fresh ULID per incident. |
| 3 | States: D4 `OPEN/ACKNOWLEDGED/RESOLVED`; surface-map adds `expired` (undefined); agent `open → updating → resolved`. | `open → acknowledged → resolved`, and `expired` = open for `max_age_s` (3600) without resolving. `updating` is a `revision` bump. Terminal states are final. |
| 4 | D4: baselines freeze "for the service" while its incident is open. | The tracker keeps **every service in an open incident** frozen, renewing their leases on each anomaly and each tick, so a quiet service in a long incident does not start learning the fault. Resolve and expire release them all. |
| 5 | Agent: "emit the first anomaly immediately, then update the same incident". Per-event anomalies would flood. | Publish on open, on a severity increase, on a new service joining, on acknowledge/resolve/expire, and otherwise at most every `update_interval_s` (30, signal time) when evidence changed. |
| 6 | Agent: severity = signal severity + count + graph distance (if available). | `critical` if any anomaly is critical, or `>= 3` services, or `>= 20` anomalies; else `warning`. Distance is a P5 refinement (non-goal above). |
| 7 | Proposal groups "logs, metrics, traces, **and alerts**"; D7 uses raw Alertmanager as the baseline we beat. | Alertmanager signals for a service already in an open incident are attached to `signals` as corroboration. They never open an incident. |
| 8 | D7 ablation `no-correlation`: "every anomaly is its own incident". | `INFRAMIND_CORRELATION_ENABLED=false` makes every anomaly open its own incident (no joining). |

## Architecture

All inside `correlation/`, replacing the internals of `IncidentTracker` and keeping its interface
(`on_anomaly`, `tick`, `open_services`) so the worker barely changes:

| Module | Responsibility |
|---|---|
| `config.py` | `CorrelationParams` (`window_s=300`, `cooldown_s=120`, `max_age_s=3600`, `update_interval_s=30`, `critical_services=3`, `critical_anomalies=20`, `enabled=True`) loaded from `INFRAMIND_CORRELATION_<PARAM>`. |
| `fingerprint.py` | `fingerprint(first_service, opened_at, window_s) -> str`. |
| `state_machine.py` | The transition table and `transition(state, event)`; illegal transitions raise. |
| `window.py` | `choose_incident(candidates, service, ts, window_s)`: which open incident an anomaly joins, if any. |
| `tracker.py` | Multi-service incidents, freeze keepalive, severity classification, throttled publishing, acknowledge, expiry, alert attachment. |
| `worker.py` | Routes Alertmanager signals to the tracker; loads `CorrelationParams`. |

`common/incident.py` gains a required `fingerprint`.

## Verification

Unit tests for each module; a tracker test for the scenario that motivated this phase (six services
firing together become one incident with six services, one freeze lease each); a flood test (900
anomalies publish a handful of snapshots); and the live gate extended with a `grouped` check:
**one** incident must cover at least three services after the burst.

## Left to the builder

Internal record layout, the exact event names in the state machine, and how snapshots are timestamped
as long as they follow signal time.
