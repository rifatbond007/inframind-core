# Surface map — InfraMind

> **Derived, and hand-kept.** What InfraMind holds — the signal contracts in, the incident
> payload out, the environment keys, the upstream services, the storage schema. It **cites
> [`decision-tree.md`](decision-tree.md) and decides nothing.** Where this file disagrees with
> shipped code, **the code is right** — fix the transcription.
>
> This is the Python/Kubernetes surface, not a web app. There are no routes and no cookies;
> there are **signals**, **incidents**, **candidates**, and **alerts**.

---

## Stage contracts at a glance

The pipeline has five stages. The contract between each pair is fixed; a stage implementation
can be swapped without changing the contract on either side.

| From | To | Stream / API | Shape |
|---|---|---|---|
| External (Prometheus, Loki, Jaeger, Alertmanager) | Ingestion | HTTP pull (PromQL / query_range / OTLP) + Alertmanager webhook | `Signal` (Pydantic, see D-`common`) |
| Ingestion | Detection + Correlation | Redis Streams (`stream:signals`) | `Signal` |
| Detection + Correlation | RCA | Redis Streams (`stream:incidents`) | `Incident` |
| RCA | LLM + Alerting | in-process (function call) | `Incident`, `Candidate[]` with `Evidence` |
| LLM + Alerting | Storage | Postgres `INSERT` (incidents, evidence, alerts, audit) | `IncidentRow`, `AlertRow` |
| Storage | API | FastAPI (`/incidents`, `/incidents/{id}`, `/healthz`, `/metrics`) | `Incident` JSON |
| External | API | `GET` | `Incident` JSON |

> **Bus names are pinned.** Renaming a Redis stream key is a D-change and breaks every running
> deployment. See `src/inframind/common/stream_keys.py` for the single source.

---

## Signals in

Every collector emits a `Signal`. The schema is in `src/inframind/common/signal.py` (D-`common`).
A short version, so this file stays the surface map:

```python
class Signal(BaseModel):
    id: str                 # ULID, set by the collector
    source: Literal["prometheus", "loki", "jaeger", "alertmanager"]
    service: str            # the service the signal is about
    kind: Literal["metric", "log", "trace", "alert"]
    timestamp: datetime     # when the signal was observed (NOT when we ingested it)
    ingested_at: datetime   # when the collector enqueued it
    value: float | None     # the metric value, error count, latency, etc. (None for traces/logs)
    labels: dict[str, str]  # free-form, source-specific (pod, severity, status code, ...)
    raw: dict | None        # the unparsed payload, for debugging. NEVER sent to the LLM.
```

**Three things are pinned:**

1. **`timestamp` is observation time, not ingestion time.** Detectors use `timestamp`. The
   `ingested_at` field exists so we can see lag.
2. **`raw` is local-only.** It is dropped before the LLM stage and not persisted.
3. **Decimals stay strings in the wire format.** Floats are fine for metrics; monetary or
   capacity-shaped values are strings until they cross the storage boundary.

### Source-specific notes

- **Prometheus** — pulled by `query_range`. Labels include `pod`, `instance`, `job`. The collector
  paginates with `--start` / `--end` / `--step`; it does **not** use a streaming pull.
- **Loki** — pulled by LogQL `query_range`. Labels include `container`, `namespace`. Log lines
  are stored in `raw`; the LLM never sees them.
- **Jaeger / OTLP** — pulled by the Jaeger HTTP API (`/api/traces`). Service names and operation
  names are kept in `labels`. Only error spans are enqueued.
- **Alertmanager** — pushed via webhook into `inframind.api.webhooks.alertmanager`. The
  webhook is the only ingress that is **not** a pull.

## Incidents out

An incident is what the world sees. A `Candidate` is one possible root cause with its evidence.
An `Alert` is the final message we send.

```python
class Incident(BaseModel):
    id: str                      # ULID
    opened_at: datetime
    closed_at: datetime | None
    state: Literal["open", "acknowledged", "resolved", "expired"]
    title: str                   # short, human-readable
    services: list[str]          # the services in the incident window
    signals: list[str]           # signal ids (no payload)
    candidates: list["Candidate"]
    primary: str | None          # candidate id of the chosen root cause
    summary: str | None          # LLM summary, post-validation
    alerts: list[str]            # alert ids sent for this incident

class Candidate(BaseModel):
    id: str
    service: str
    score: float                 # the ranking score (deterministic, see D15)
    evidence: list["Evidence"]   # the bundle used to score it

class Evidence(BaseModel):
    kind: Literal["metric", "log_ref", "trace", "change", "baseline"]
    summary: str                 # one line, written by the collector
    ref: str                     # pointer back to the source signal id
    weight: float                # how much this evidence moved the score

class Alert(BaseModel):
    id: str
    incident_id: str
    severity: Literal["info", "warning", "critical"]
    channel: Literal["slack", "email", "webhook"]  # extensible, see D-`alerting`
    payload: dict                # the rendered, redacted, deduped payload
    sent_at: datetime
```

**The wire shape of an alert is the alert's problem, not the schema's.** The renderer is in
`inframind.alerting.render`. Routing by severity is in `inframind.alerting.router`.

---

## Environment keys

Read once at boot by `src/inframind/common/config.py` (a pydantic-settings class). The
process **refuses to start** if a required key is missing.

| Key | Required | Default | Purpose |
|---|---|---|---|
| `REDIS_URL` | yes | `redis://localhost:6379/0` | Streams + dedup keys |
| `POSTGRES_URL` | yes | `postgres://inframind:inframind@localhost:5432/inframind` | Incident store |
| `LLM_PROVIDER` | yes | `OPENAI` | `OPENAI` or `OLLAMA` |
| `LLM_API_KEY` | when provider is `OPENAI` | — | Redacted before any log line |
| `LLM_MODEL` | no | `gpt-4o-mini` | Model name |
| `OLLAMA_HOST` | when provider is `OLLAMA` | `http://localhost:11434` | Local fallback |
| `INFRAMIND_ENV` | yes | `dev` | `dev` / `test` / `prod` — gates redaction strictness |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `BASELINE_FREEZE_WINDOW` | no | `300` | seconds — D4 decay window |
| `DETECTION_CONCURRENCY` | no | `4` | per-stage worker count |
| `RCA_GRAPH_PATH` | no | `data/service-graph.json` | Built by the testbed engineer |
| `EVAL_SEED` | only in `test` | `0` | Reproducibility for evaluation (D6) |

`.env.example` carries the same list with empty values. The real `.env` is gitignored.

---

## The upstream contract

There is no web frontend. The "upstreams" are the observability backends and the test app.

| Upstream | What we read | Auth |
|---|---|---|
| Prometheus (`kube-prometheus-stack`) | PromQL `query_range` over the cluster's metrics | in-cluster service token, mountable to local via `kubectl port-forward` |
| Loki | LogQL `query_range` over the cluster's logs | same |
| Jaeger | HTTP `/api/traces` over erroring traces | same |
| Alertmanager | webhook POST from the in-cluster Alertmanager | shared header secret, see `INFRAMIND_ALERT_WEBHOOK_SECRET` |
| Online Boutique / OTel Demo | none — we observe, we do not call | — |
| Chaos Mesh | we **apply** CRs in `evaluation/`, not in production | kubeconfig scoped to `kind-*` contexts only |

The contract that matters for the testbed is: **everything InfraMind needs is reachable from
inside the cluster through a service DNS name, or from the host through `kubectl
port-forward`.** No outbound internet access is required to run the system.

---

## Storage schema (Postgres)

The schema is owned by the alerting-storage engineer. Migrations live in
`src/inframind/storage/migrations/`. Tables:

| Table | Purpose | Notes |
|---|---|---|
| `incidents` | one row per incident | `id` ULID, `state` enum, `opened_at`, `closed_at` |
| `candidates` | one row per candidate | FK to `incidents.id`, indexed on `(incident_id, score DESC)` |
| `evidence` | one row per evidence item | FK to `candidates.id`, payload is JSONB |
| `alerts` | one row per alert sent | FK to `incidents.id`, redacted payload stored |
| `audit_log` | append-only | every state change, every external call. No `UPDATE`, no `DELETE` |

`audit_log` is the one table that is write-only. The only API on it is `INSERT` and `SELECT`.

---

## API surface

FastAPI app at `src/inframind/api/app.py`. Routes are the only public HTTP surface.

| Route | Method | Purpose |
|---|---|---|
| `/healthz` | GET | liveness — `{"status": "ok"}` |
| `/readyz` | GET | readiness — checks Redis and Postgres, returns 503 on either failing |
| `/metrics` | GET | Prometheus exposition for the InfraMind process itself |
| `/webhooks/alertmanager` | POST | the only ingress webhook. Validates `INFRAMIND_ALERT_WEBHOOK_SECRET` |
| `/incidents` | GET | list, paginated, supports `?state`, `?service`, `?since`, `?limit`, `?cursor` |
| `/incidents/{id}` | GET | one incident with candidates, evidence, and alerts |
| `/incidents/{id}/ack` | POST | mark acknowledged |
| `/incidents/{id}/resolve` | POST | mark resolved (used by humans, not by the loop) |

No PATCH, no DELETE. State transitions are explicit.

---

## What is intentionally not in this surface

- No DL model endpoints. D3.
- No webhooks for the LLM, no inbound HTTP from third parties, no auto-remediation endpoints. D8.
- No direct database access from outside the cluster. The API is the only door.
- No long-running HTTP connections; the Alertmanager webhook is the only POST the API accepts.