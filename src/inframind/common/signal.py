"""The canonical `Signal`: every collector maps its backend payload to this before publishing."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .ids import new_ulid
from .logging import get_logger

log = get_logger(__name__)

UNKNOWN_SERVICE = "unknown"
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class Source(StrEnum):
    """Which collector produced the signal."""

    PROMETHEUS = "prometheus"
    LOKI = "loki"
    JAEGER = "jaeger"
    OTEL = "otel"
    ALERTMANAGER = "alertmanager"


class SignalType(StrEnum):
    """What kind of observation the signal carries."""

    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    ALERT = "alert"


class Severity(StrEnum):
    """Coarse classification; the detector refines it into an Anomaly."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


def normalise_service(name: str) -> str:
    """Return the canonical lower-kebab-case service name, or `unknown` if nothing is left."""
    cleaned = re.sub(r"[^a-z0-9-]", "-", name.strip().lower()).strip("-")
    return cleaned or UNKNOWN_SERVICE


class Signal(BaseModel):
    """One observation on the bus. `ts` is observation time, never ingestion time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=new_ulid)
    ts: datetime
    source: Source
    type: SignalType
    service: str
    severity: Severity = Severity.INFO
    trace_id: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts", mode="before")
    @classmethod
    def _ts_is_datetime_or_iso(cls, v: Any) -> Any:
        if isinstance(v, int | float):  # bool is an int subclass
            raise ValueError("ts must be a datetime or ISO-8601 string, not an epoch number")
        return v

    @field_validator("ts")
    @classmethod
    def _ts_is_aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("ts must be timezone-aware (use inframind.common.time.utcnow())")
        return v.astimezone(UTC)

    @field_validator("service")
    @classmethod
    def _service_canonical(cls, v: str) -> str:
        return normalise_service(v)

    @field_validator("trace_id")
    @classmethod
    def _trace_id_w3c(cls, v: str | None) -> str | None:
        if v is not None and not _TRACE_ID_RE.match(v):
            raise ValueError("trace_id must match ^[0-9a-f]{32}$")
        return v

    @field_validator("attrs")
    @classmethod
    def _attrs_json_only(cls, v: dict[str, Any]) -> dict[str, Any]:
        kept: dict[str, Any] = {}
        for key, value in v.items():
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError):
                log.debug("attr_dropped_not_json", attr=str(key))
                continue
            kept[str(key)] = value
        return kept

    @property
    def is_routable(self) -> bool:
        """False when the service could not be resolved; the bus drops such signals."""
        return self.service != UNKNOWN_SERVICE
