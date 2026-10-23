"""Window grouping: which open incident an anomaly belongs to.

The proposal groups signals that "share the same service or fall within a configurable window
(default 5 minutes)". So an anomaly joins an open incident when its service is already in it,
and otherwise when it lands within `window_s` of that incident's last anomaly. The same service
wins, then the most recently active incident. Existing incidents are never merged.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, TypeVar


class Candidate(Protocol):
    """What the grouping needs to know about an open incident."""

    @property
    def services(self) -> frozenset[str]:
        """The services already in the incident."""

    @property
    def last_anomaly_ts(self) -> datetime:
        """Observation time of the incident's newest anomaly."""


T = TypeVar("T", bound=Candidate)


def choose_incident(
    candidates: Sequence[T], service: str, ts: datetime, window_s: float
) -> T | None:
    """Return the open incident an anomaly for `service` at `ts` joins, or None to open a new one."""
    same_service = [c for c in candidates if service in c.services]
    if same_service:
        return max(same_service, key=lambda c: c.last_anomaly_ts)
    nearby = [c for c in candidates if abs((ts - c.last_anomaly_ts).total_seconds()) <= window_s]
    if nearby:
        return max(nearby, key=lambda c: c.last_anomaly_ts)
    return None
