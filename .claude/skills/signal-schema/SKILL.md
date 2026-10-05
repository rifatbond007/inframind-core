---
name: signal-schema
description: The unified Signal Pydantic model. Source of truth for everything that flows through the pipeline.
---

# Signal schema

`Signal` is the canonical representation of every observation that enters the pipeline.

## Definition (Pydantic)

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
    ts: datetime  # UTC
    source: Source
    type: SignalType
    service: str  # canonical service name
    severity: Severity = Severity.INFO
    trace_id: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
```

## Field semantics

- `id` — globally unique. Used by the bus consumer for dedup.
- `ts` — wall-clock UTC. Set by the collector at observation time, not at publish time.
- `source` — which collector produced the signal.
- `type` — what kind of signal.
- `service` — canonical service name. Same value across collectors for the same microservice.
- `severity` — coarse classification. Refined downstream by detectors.
- `trace_id` — OTel / W3C trace context ID. Optional.
- `attrs` — everything else. Must be JSON-serializable.

## Constraints

- `service` MUST NOT be empty. Validators normalize to lower-kebab-case.
- `attrs` MUST be JSON-serializable. Non-serializable values are dropped with a debug log.
- Never include PII / secrets in `attrs`. Redact before publish (D9).

## Validation rules

- `ts` must be a real datetime (not Unix epoch integer). Convert in the collector.
- `service` is normalized: `re.sub(r"[^a-z0-9-]", "-", name.lower())`.
- If `trace_id` is present, it must match the W3C trace-context format (`^[0-9a-f]{32}$`).

## How to use

Every collector maps its backend payload to `Signal` and publishes to the bus. No backend payload ever crosses a module boundary.