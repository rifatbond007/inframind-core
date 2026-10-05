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

A Prometheus `query_range` returns rows like:

```json
{
  "metric": {"__name__": "http_requests_total", "service": "frontend", "code": "500"},
  "values": [[1704067200, "12"], [1704067215, "13"]]
}
```

The collector unpacks the row, picks the timestamp, the value, and the labels, then maps:

```python
def _to_signal(self, raw: dict[str, Any]) -> Signal:
    metric = raw["metric"]
    ts_unix, value_str = raw["values"][-1]
    return Signal(
        ts=datetime.fromtimestamp(ts_unix, tz=timezone.utc),
        source=Source.PROMETHEUS,
        type=SignalType.METRIC,
        service=self._normalise_service(metric["service"]),
        severity=self._severity_for(metric.get("code", "")),
        attrs={
            "metric": metric["__name__"],
            "value": float(value_str),
            "labels": {k: v for k, v in metric.items() if k not in ("__name__", "service")},
            "ingested_at": utcnow().isoformat(),
        },
    )
```

Two points:

1. `service` is normalised. `frontend` stays `frontend`; `FrontEnd` becomes `frontend`.
3. `attrs["value"]` is a `float`. Monetary values stay strings until they cross the storage
   boundary (`storage/`), where the column type is `NUMERIC(20,4)`.

## 3.7 Worked example: Alertmanager webhook -> `Signal`

The webhook hits `/webhooks/alertmanager` in `api/`. The handler maps:

```python
def alert_to_signal(payload: dict[str, Any], request_id: str) -> Signal:
    alert = payload["alerts"][0]
    labels = alert.get("labels", {})
    return Signal(
        ts=parser.parse(alert["startsAt"]),
        source=Source.ALERTMANAGER,
        type=SignalType.ALERT,
        service=normalise_service(labels.get("service", labels.get("job", "unknown"))),
        severity=_severity_from_status(alert.get("status", "firing")),
        attrs={
            "alertname": labels.get("alertname", ""),
            "labels": labels,
            "annotations": alert.get("annotations", {}),
            "request_id": request_id,  # used for redaction-trace audit
        },
    )
```

The webhook is the only **push** ingress. Every other collector is a pull.

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
