# 3. Signal schema — the canonical observation
> standards · Python conventions · InfraMind. Pairs with the `signal-schema` skill in `.claude/skills/signal-schema/SKILL.md`. This file is the rationale.

## 3.1 What a `Signal` is

Every collector — Prometheus, Loki, Jaeger / OTel, Alertmanager — maps its backend payload to a
`Signal` and publishes it to the Redis stream. The bus is typed. No raw backend object crosses
a module boundary.

```python
from inframind.common.signal import Signal
```

## 3.2 The shape

```python
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class Source(str, Enum):
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    JAEGER = "jaeger"
    OTEL = "otel"
    ALERTMANAGER = "alertmanager"


class SignalType(str, Enum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    ALERT = "alert"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Signal(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    ts: datetime  # UTC, observation time
    source: Source
    type: SignalType
    service: str  # canonical, lower-kebab-case
    severity: Severity = Severity.INFO
    trace_id: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
```

## 3.3 Field semantics — and the WHY

| Field | Type | WHY this shape |
|---|---|---|
| `id` | ULID, set by the collector | Globally unique; bus consumer uses it for dedup. |
| `ts` | `datetime` UTC, observation time | The detector compares on observation time. `when2` (ingestion time) is not stored on `Signal` — see `attrs["ingested_at"]` if you need it. A Unix epoch integer is rejected by the validator. |
| `source` | `Source` enum | One of five collectors. Adding a sixth needs a D-number. |
| `type` | `SignalType` enum | What kind of signal — drives the consumer that picks it up. |
| `service` | canonical `str` | Lower-kebab-case. `re.sub(r"[^a-z0-9-]", "-", name.lower())` on the way in. Two collectors writing the same microservice MUST agree on the spelling. |
| `severity` | `Severity` enum | Coarse classification. The detector refines this into an `Anomaly`. |
| `trace_id` | optional W3C format | `^[0-9a-f]{32}$` when present. Lets the correlation stage join across sources. |
| `attrs` | JSON-serializable dict | Everything else. Must be JSON-serializable — non-serializable values are dropped with a debug log. Never carries PII or secrets (D9). |

## 3.4 Constraints (hard rules)

- **`service` MUST NOT be empty.** Validators normalise to lower-kebab-case; empty becomes
  `unknown` and the signal is dropped with a warning.
- **`attrs` MUST be JSON-serializable.** A `bytes` value, a `datetime` not converted to ISO, a
  custom object — all dropped, never sent to the LLM.
- **No secrets in `attrs`.** D9. The collector runs a redactor over known shapes (JWT, bearer,
  email, IP, env var) before the value reaches the bus.
- **`trace_id` format** — when present, must match the W3C trace-context format
  (`^[0-9a-f]{32}$`). Mismatch means the consumer drops the field.
- **`ts` timezone** — `Signal` validators reject a naive `datetime`. Always pass
  `datetime.now(timezone.utc)` (or `datetime.utcnow()` replaced by `inframind.common.time.utcnow()`).

## 3.5 Where it lives in code

```
src/inframind/common/
├── signal.py        # the model
├── time.py          # the utcnow() helper
└── stream_keys.py   # the pinned bus key (see 1.4)
```

## 3.6 Worked example: Prometheus payload -> `Signal`

Online Boutique's services are gRPC and expose no per-service request metrics (no
`http_requests_total{service=…}`), so the Prometheus collector reads per-pod resource and restart
series from cAdvisor and kube-state-metrics. A `query_range` returns rows like:

```json
{
  "metric": {"namespace": "default", "pod": "frontend-5c744dd6bb-jfpxt"},
  "values": [[1704067200, "0.012"], [1704067215, "0.014"]]
}
```

The collector maps the latest sample (`src/inframind/ingestion/prometheus.py`, `row_to_signal`):

```python
def row_to_signal(name: str, row: dict[str, Any], *, ingested_at: datetime) -> Signal | None:
    labels = row.get("metric", {})
    ts_unix, value_str = row["values"][-1]
    return Signal(
        ts=datetime.fromtimestamp(float(ts_unix), tz=UTC),
        source=Source.PROMETHEUS,
        type=SignalType.METRIC,
        service=service_from_pod(labels["pod"]),
        severity=Severity.INFO,
        attrs={
            "metric": name,  # e.g. "cpu_cores"
            "value": float(value_str),
            "labels": {k: v for k, v in labels.items() if k != "__name__"},
            "ingested_at": ingested_at.isoformat(),
        },
    )
```

Three points:

1. `service` comes from the pod name via `service_from_pod` (in `common/signal.py`), which strips
   ReplicaSet and StatefulSet suffixes: `frontend-5c744dd6bb-jfpxt` becomes `frontend`. DaemonSet
   and control-plane static pods (`kindnet-ab12c`, `etcd-<node>`) are kept whole.
2. `attrs["value"]` is a `float`. Monetary values stay strings until they cross the storage
   boundary (`storage/`), where the column type is `NUMERIC(20,4)`.
3. Series with a non-finite latest value (`NaN`, `Inf`) or without a `pod` label produce no signal.

## 3.7 Worked example: Alertmanager webhook -> `Signal`

The webhook is `POST /webhooks/alertmanager` in `src/inframind/api/alertmanager.py` (only `api/`
imports FastAPI). It requires `Authorization: Bearer <INFRAMIND_ALERT_WEBHOOK_SECRET>`. One
notification carries a *group* of alerts, and **every** alert becomes its own `Signal` via the pure
`alerts_to_signals(payload, request_id=…, ingested_at=…)`:

- `ts` is the alert's `startsAt`; for a `resolved` alert it is `endsAt`.
- `severity` is the `severity` label (`critical` or `warning`); a resolved alert is `info`.
- `service` is resolved in this order: `deployment` label, then `pod` label (through
  `service_from_pod`), then `service` label, then `job`.
- `attrs` carry `alertname`, `status`, `fingerprint`, `labels`, `annotations` (redacted),
  `starts_at`, `ends_at`, `group_key` and `request_id`.

**Why `service` is not first.** The Prometheus Operator stamps `service` with the Service of the
*scrape target*, so every pod-level alert derived from kube-state-metrics carries
`service="kube-prometheus-stack-kube-state-metrics"`. Trusting that label would blame the
exporter instead of the workload; the `pod` and `deployment` labels name the affected workload.

An alert that resolves to `unknown` (the always-firing `Watchdog`) is dropped by the bus and
counted in the response `{"accepted": n, "dropped": m}`. Alertmanager retries on a non-2xx, so a
bus failure returns 503; a retried group repeats the same `fingerprint`, `starts_at` and `status`,
which is the key downstream stages deduplicate on. The webhook is the only **push** ingress.
Every other collector is a pull.

## 3.8 Hard rules

- **`Signal.id` is set once, by the collector.** Downstream stages never re-set it.
- **The collector never sends a raw backend object downstream.** It maps to `Signal` first.
  Anyone who touches raw Prometheus / Loki / Jaeger JSON in `detection/` or `correlation/` is
  breaking the contract.
- **`ts` is observation time.** If a collector cannot resolve it, it omits the signal and logs a
  warning. Publishing a signal with `ts=ingest_time` will silently bias every detector.

## 3.9 Cross-reference

- **Skill:** `.claude/skills/signal-schema/SKILL.md`.
- **Locked decisions:** `docs/decision-tree.md` — D9 (no secrets/PII in `attrs`); D20 (storage
  is the source of truth, the bus is the conduit).
- **Upstream contract:** `docs/surface-map.md` — "Signals in" section.
- **Adding a collector:** section 04 in this set.
