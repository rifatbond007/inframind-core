# 14. Change correlation — the RCA `w4` term and the K8s change watcher
> standards · Architecture · InfraMind. Pairs with the `rca-scoring` skill in `.claude/skills/rca-scoring/SKILL.md` (consumes the score) and the `helm-packaging` skill (the RBAC that grants the watcher read access). This file pins the algorithm and the data source. D22.

## 14.1 Why this standard exists

The RCA scoring formula in section 6.4 has four terms. The first three are well-specified: `anomaly_severity`, `earliest_onset`, `downstream_depth`. The fourth — `change_correlation` — is named and weighted, but until this standard lands it has no algorithm and no data source. Without a pinned spec, every implementation invents its own and the paper's reproducibility contract (D6) breaks at the `w4` term.

This standard fixes:

- **What a "change event" is** — the resource set, the field filter, the metadata captured.
- **Where change events come from** — a single source: the K8s API watch stream.
- **How a change event correlates with a candidate service** — the per-candidate scoring function.
- **The time window** — a 30-minute pre-window before the incident opens.
- **What is never stored** — Secret values, request bodies, raw env values (D9).

## 14.2 What counts as a "change event"

A change event is any state mutation to a service or its runtime configuration that could plausibly cause a fault. The pinned set:

| Event type | K8s resource | What we record |
|---|---|---|
| Deployment rollout | `apps/v1 Deployment` (`.spec.template` change) | service name, revision, image, change time |
| ConfigMap change | `v1 ConfigMap` (`.data` change) | service name (via owner ref or label selector), configmap name, change time |
| Secret change | `v1 Secret` (`.data` change) | service name (via owner ref or label selector), secret name, change time, **never the value** |
| HPA scaling | `autoscaling/v2 HorizontalPodAutoscaler` (`.spec.replicas` change) | service name, old replicas, new replicas, change time |
| StatefulSet rollout | `apps/v1 StatefulSet` (`.spec.template` change) | service name, revision, change time |
| DaemonSet rollout | `apps/v1 DaemonSet` (`.spec.template` change) | service name, change time |

**Out of scope** (D8): security events (RBAC, NetworkPolicy, ServiceAccount token rotations), persistent volume changes, node-level changes, anything outside the namespaces listed in `INFRAMIND_WATCH_NAMESPACES`.

**Filter at the watcher:**

- For `Deployment` / `StatefulSet` / `DaemonSet`: only changes to `.spec.template` count. Status updates do not.
- For `ConfigMap` / `Secret`: only changes to `.data` count. Annotations and labels do not.
- For `HPA`: only changes to `.spec.replicas` count. Status updates do not.

A rolling update that creates 5 new pods is **one** event, not five. The watcher deduplicates by `(resource, revision|resourceVersion)`.

## 14.3 Data source — a single K8s API watch

A single consumer: the **`inframind-change-watcher`** (a small component that lives in the RCA worker pod — section 14.4). It opens watch streams on the resources listed in section 14.2, in the namespaces listed in `INFRAMIND_WATCH_NAMESPACES`.

| Setting | Default | Env key |
|---|---|---|
| Watched namespaces | the namespaces of the system-under-test (e.g. `default`, `inframind`) | `INFRAMIND_WATCH_NAMESPACES` (comma-separated) |
| Watch timeout | 5 minutes (then resume) | `K8S_WATCH_TIMEOUT_S` |
| Retry backoff | exponential, 1s -> 60s | n/a (internal) |

### Why the K8s API and not the audit log

The audit log is verbose (every API call, including reads) and requires read access to `events` resources cluster-wide, which is more privileged than needed. The K8s API watch on the specific resource types is sufficient, scoped, and matches the existing read-only RBAC scope from section 11.5 (extended in section 14.6 to include `secrets`).

### Why a single watcher, not one per pod

- **Single source of truth.** Two watchers would race on the `change_events` Postgres table and on the in-memory ring buffer.
- **Resource cost.** A watch is cheap; running N of them per N RCA replicas is wasteful when one watcher can serve all consumers via the Postgres table.
- **Failure domain.** The watcher is part of the RCA worker. It inherits the RCA worker's lifecycle (start, restart, graceful shutdown) without a new service to operate.

## 14.4 Where the watcher runs

The watcher is **in-process** in the RCA worker pod (`src/inframind/rca/change_watcher.py`, P5). It does not run in its own `Deployment`. The reasons:

- It shares the RCA worker's K8s `ServiceAccount` (no new RBAC plumbing beyond the `secrets` extension in section 14.6).
- It shares the RCA worker's Redis client and Postgres pool.
- Its output is consumed only by the RCA ranker, which lives in the same process.

If the project ever scales to multiple RCA replicas, the watcher becomes a separate `Deployment` with one replica. That is a D-change.

## 14.5 The Postgres table

```sql
CREATE TABLE change_events (
    id            BIGSERIAL PRIMARY KEY,
    occurred_at   TIMESTAMPTZ NOT NULL,
    service       TEXT NOT NULL,                  -- canonical service name (lower-kebab)
    kind          TEXT NOT NULL,                  -- 'deployment_rollout' | 'configmap_change' | 'secret_change' | 'hpa_change' | 'statefulset_rollout' | 'daemonset_rollout'
    resource      TEXT NOT NULL,                  -- 'deploy/frontend', 'cm/checkout-config', 'secret/db-credentials', etc.
    revision      TEXT,                           -- Deployment revision, ConfigMap resourceVersion, etc.
    prev_revision TEXT,                           -- for diff
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX change_events_service_time_idx
    ON change_events (service, occurred_at DESC);
```

**No `payload` column. No `data` column. No `value` column.** The `Secret` value is never persisted. Only metadata: which Secret changed, in which namespace, at what time, identified by which revision.

The table is append-only. No `UPDATE`, no `DELETE` (D20 — the audit log discipline applies to the change log too).

## 14.6 The RBAC extension

The change watcher needs `get` / `list` / `watch` on the resources in section 14.2. The existing read-only `Role` in `11-helm-packaging.md` section 11.5 grants:

- `pods`, `pods/log` (existing)
- `events` (existing)
- `configmaps` (existing)

D22 extends the same `Role` (no new `Role`, no new `ServiceAccount`) to add:

- `secrets` — `get`, `list`, `watch`

The `Role` remains read-only. No `create`, no `update`, no `delete`, no `patch`, no `exec`. A reviewer who sees a write verb in the change-watcher's `Role` rejects the PR.

The K8s API client uses the same `ServiceAccount` token the RCA worker already mounts. No new volume mounts, no new env keys.

## 14.7 The correlation algorithm

When the RCA stage receives an `Incident` and walks the graph to produce candidates, the change-correlation step runs **per candidate** after the walk, before scoring.

```python
# Interface only — implementation lives in src/inframind/rca/change_correlation.py (P5).

def change_correlation(
    candidate_service: str,
    incident_window: tuple[datetime, datetime],
    change_events: Sequence[ChangeEvent],
    pre_window_s: int = 1800,        # CHANGE_CORRELATION_PRE_WINDOW_S
) -> float:
    """Return a score in [0, 1].

    0.0 = no relevant change events in the window.
    1.0 = a strong, recent, on-target change event.
    """
```

**Algorithm (pinned):**

1. Filter `change_events` to those on `candidate_service` whose `occurred_at` is in `[incident.opened_at - pre_window_s, incident.closed_at]`. The pre-window default is 1800 s (30 minutes), configurable via `CHANGE_CORRELATION_PRE_WINDOW_S`. The post-window extends to `incident.closed_at` to catch late-arriving rollouts (e.g. an HPA scale-up that fires after the incident opens).
2. For each filtered event, compute a **recency weight**:
   ```
   recency = max(0.0, 1.0 - (delta_s / pre_window_s))
   ```
   where `delta_s` is the absolute time from the event to `incident.opened_at`. Events closer to the incident open time weight more.
3. **Sum** the recency weights. Call this `raw`.
4. **Soft-saturate** the sum so multiple events on the same service don't dominate the score:
   ```
   normalised = raw / (1.0 + 0.5 * (n_events - 1))   for n_events >= 1
   normalised = 0.0                                for n_events == 0
   ```
   The 0.5 factor means two events on the same service count as 1.5× one event, three as 2.0×, four as 2.5×, etc. A noisy service with 10 rollouts in the window caps at 5.5×.
5. **Cap at 1.0.**
6. If no events in the window, return `0.0`.

**Worked example:**

- Incident opens at `T = 100s`.
- `frontend` had a Deployment rollout at `T = 60s` and a ConfigMap change at `T = 95s`.
- Pre-window: `[T-30s, T] = [70s, 100s]`.
- Recency weights:
  - Deployment at T-40s: outside the window (T < 70s). `recency = 0.0`.
  - ConfigMap at T-5s: `recency = 1 - 5/30 = 0.833`.
- `raw = 0.833`. `n_events = 1`. `normalised = 0.833 / (1 + 0.5 * 0) = 0.833`.
- `change_correlation(frontend) = 0.833`.

Other candidates without recent changes score `0.0` and receive no `w4` boost.

## 14.8 Where this fits in the RCA pipeline

```
[Incident]
   -> [walk_toward_callees]                          # section 6.3
   -> [per_candidate: change_correlation(...)]       # THIS STANDARD
   -> [evidence bundle]                              # section 6.6
   -> [score = w1*... + w2*... + w3*... + w4*change_correlation]  # section 6.4
   -> [rank]
```

The `change_correlation` step reads from the in-memory `change_events` ring buffer (hot path) with a Postgres fallback (cold path) if the buffer is empty (e.g. the RCA worker just restarted). The Postgres query is indexed on `(service, occurred_at DESC)` (section 14.5), so the fallback is fast.

## 14.9 Hard rules

- **D9 — no Secret values stored.** The change watcher reads `Secret` metadata only. The `change_events` table has no `value` column. A reviewer who sees `Secret.data` or `Secret.stringData` referenced in the watcher's code rejects the PR.
- **D9 — no PII, no env values, no request bodies.** The watcher is metadata-only by construction.
- **D2 / RBAC — read-only.** The watcher's `Role` (section 14.6) is the existing read-only `Role`, extended with `secrets` get/list/watch. No write verbs. No `pods/exec`.
- **D6 — reproducibility.** The change log is part of the per-scenario reproducibility contract. Given `scenario_id + seed`, the time of the rollout is part of the scenario YAML. The runner script applies the rollout at a specific time; the watcher records it; the result CSV records `change_correlation_score` per candidate.
- **One change event per K8s mutation, not per object.** A rolling update with 5 new pods is one event. The watcher deduplicates by `(resource, revision|resourceVersion)`.
- **No retroactive edits.** A change event is recorded when the API notifies the watcher. The watcher does not poll historical state.
- **D22 — namespaces are scoped.** `INFRAMIND_WATCH_NAMESPACES` is mandatory. A watcher that defaults to "all namespaces" is wrong.
- **D22 — append-only.** The `change_events` table is never updated, never deleted. The audit-log discipline (D20) applies.

## 14.10 Tests

```
tests/unit/rca/test_change_correlation.py
    # synthetic change events + known incident windows
    # assert: 0 events -> 0.0
    # assert: 1 event at T-5s in a 30s pre-window -> ~0.83
    # assert: cap at 1.0
    # assert: events outside the pre-window are ignored
    # assert: 3 events soft-saturate correctly

tests/unit/rca/test_change_watcher_filter.py
    # Deployment status update -> not an event
    # ConfigMap annotation change -> not an event
    # Deployment .spec.template change -> one event
    # ConfigMap .data change -> one event
    # Secret .data change -> one event, no value

tests/integration/test_change_watcher_live.py
    # on the testbed: apply a ConfigMap, assert the change event lands
    # in Postgres within 2 s, and the change_correlation score for the
    # corresponding service moves from 0.0 to > 0.5
```

## 14.11 Cross-reference

- **Locked decision:** `docs/decision-tree.md` — D2 (graph orientation), D6 (reproducibility), D9 (no secrets in LLM-bound data), D20 (append-only audit log), D22 (this standard).
- **Consumed by:** `06-rca-scoring.md` section 6.4 — the `w4` term in the scoring formula.
- **Consumes:** the K8s API and the `change_events` Postgres table.
- **RBAC:** `11-helm-packaging.md` section 11.5 — the read-only `Role` extended with `secrets` get/list/watch.
- **Skill:** `.claude/skills/rca-scoring/SKILL.md` (procedural entrypoint).
