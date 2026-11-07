"""`Anomaly`: one detector firing on one signal. The unit the correlation stage groups.

It lives in `common/` rather than `detection/` because the RCA stage (which may import only
`common`) receives anomalies inside `Incident`s on `stream:incidents`. `detection.anomaly`
re-exports it so the import path in standard 05 works.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .ids import new_ulid
from .signal import UNKNOWN_SERVICE, Severity, normalise_service


class Anomaly(BaseModel):
    """A detector decided signal `signal_id` is anomalous. `ts` is the signal's observation time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=new_ulid)
    ts: datetime
    detector: str
    service: str
    signal_id: str
    score: float = Field(allow_inf_nan=False)
    severity: Severity
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
            raise ValueError("ts must be timezone-aware")
        return v.astimezone(UTC)

    @field_validator("service")
    @classmethod
    def _service_canonical(cls, v: str) -> str:
        service = normalise_service(v)
        if service == UNKNOWN_SERVICE:
            raise ValueError("service must not be empty or unknown")
        return service

    @field_validator("attrs")
    @classmethod
    def _attrs_json_only(cls, v: dict[str, Any]) -> dict[str, Any]:
        kept: dict[str, Any] = {}
        for key, value in v.items():
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError):
                continue
            kept[str(key)] = value
        return kept
