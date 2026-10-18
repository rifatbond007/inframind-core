"""`Incident`: the wire shape on `stream:incidents` (surface-map "Incidents out", pre-RCA).

States follow the surface map: `open`, `acknowledged`, `resolved`, `expired`. "Updating" (the
correlation agent's word) is not a state: a republish of the same incident bumps `revision`.
RCA outputs (candidates, summary, alerts) are added downstream; they are not on this stream.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .anomaly import Anomaly
from .ids import new_ulid
from .signal import UNKNOWN_SERVICE, Severity, normalise_service

IncidentState = Literal["open", "acknowledged", "resolved", "expired"]
_CLOSED_STATES = ("resolved", "expired")
_FINGERPRINT = re.compile(r"^[0-9a-f]{8,64}$")


class Incident(BaseModel):
    """A snapshot of one incident. Each publish is a new `revision` of the same `id`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=new_ulid)
    revision: int = Field(ge=1)
    state: IncidentState
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    title: str
    services: list[str] = Field(min_length=1)
    signals: list[str] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    severity: Severity
    fingerprint: str

    @field_validator("fingerprint")
    @classmethod
    def _fingerprint_is_lowercase_hex(cls, v: str) -> str:
        if not _FINGERPRINT.match(v):
            raise ValueError("fingerprint must be 8 to 64 lower-case hex characters")
        return v

    @field_validator("opened_at", "updated_at", "closed_at", mode="before")
    @classmethod
    def _not_an_epoch(cls, v: Any) -> Any:
        if isinstance(v, int | float):  # bool is an int subclass
            raise ValueError("timestamps must be datetimes or ISO-8601 strings, not epoch numbers")
        return v

    @field_validator("opened_at", "updated_at", "closed_at")
    @classmethod
    def _aware_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return v.astimezone(UTC)

    @field_validator("services")
    @classmethod
    def _services_canonical(cls, v: list[str]) -> list[str]:
        names = {normalise_service(s) for s in v} - {UNKNOWN_SERVICE}
        if not names:
            raise ValueError("services must contain at least one known service")
        return sorted(names)

    @model_validator(mode="after")
    def _closed_at_matches_state(self) -> Incident:
        closed = self.state in _CLOSED_STATES
        if closed and self.closed_at is None:
            raise ValueError(f"closed_at is required when state is {self.state}")
        if not closed and self.closed_at is not None:
            raise ValueError(f"closed_at must be empty when state is {self.state}")
        return self
