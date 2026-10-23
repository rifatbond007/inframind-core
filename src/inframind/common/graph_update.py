"""`GraphUpdate`: one observed caller -> callee call, the wire shape of `stream:graph-updates`.

The ingestion collectors emit these; the RCA worker is the only consumer and the only owner of
the `ServiceGraph` built from them (D21, standard 13). Edges are `caller -> callee` (D2). The
collector reports raw samples; edge weighting, EWMA and ageing are the RCA worker's job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .signal import UNKNOWN_SERVICE, normalise_service


class GraphUpdate(BaseModel):
    """One observed call from `caller` to `callee` that took `latency_ms` at `observed_at`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: str
    callee: str
    latency_ms: float = Field(ge=0, allow_inf_nan=False)
    observed_at: datetime

    @field_validator("caller", "callee")
    @classmethod
    def _service_canonical(cls, v: str) -> str:
        service = normalise_service(v)
        if service == UNKNOWN_SERVICE:
            raise ValueError("service must not be empty or unknown")
        return service

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed_at_is_datetime_or_iso(cls, v: Any) -> Any:
        if isinstance(v, int | float):  # bool is an int subclass
            raise ValueError("observed_at must be a datetime or ISO-8601 string, not an epoch")
        return v

    @field_validator("observed_at")
    @classmethod
    def _observed_at_is_aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _no_self_loop(self) -> GraphUpdate:
        if self.caller == self.callee:
            raise ValueError("caller and callee must differ")
        return self
