"""Per-incident dedup: one alert per open incident (surface-map + agent spec).

Within the dedup window, `should_alert` returns `True` exactly once per `incident_id`
unless the severity escalates — when it does, the alert can fire again so the page hits
harder. Severity `critical` is the only escalation that resets the timer; `info` and
`warning` do not.

The state is **process-local** by design: the durable mirror of "did we alert" is the
`alerts` table in Postgres (storage/incidents.py write), so a process restart can
re-fire an alert but can never *miss* one. The dedup tracker exists to keep two
consecutive rank outputs from the same incident flooding the channel.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from inframind.common.logging import get_logger

logger = get_logger(__name__)

_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


class DedupTracker:
    """One alert per open incident; severity escalation re-arms."""

    def __init__(
        self,
        *,
        window_s: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """`clock` is injectable for tests; production callers leave it None."""
        self._window_s = window_s
        self._last_sent: dict[str, tuple[float, str]] = {}
        self._clock: Callable[[], float] = clock or time.monotonic

    def should_alert(self, incident_id: str, severity: str) -> bool:
        """Return True iff an alert should fire now for `incident_id`."""
        now = self._clock()
        entry = self._last_sent.get(incident_id)
        if entry is None:
            return True
        sent_at, sent_severity = entry
        if now - sent_at >= self._window_s:
            return True
        return _SEVERITY_ORDER.get(severity, 0) > _SEVERITY_ORDER.get(sent_severity, 0)

    def record(self, incident_id: str, severity: str) -> None:
        """Mark that we just sent an alert for `incident_id` at `severity`."""
        self._last_sent[incident_id] = (self._clock(), severity)

    def forget(self, incident_id: str) -> None:
        """Drop state for `incident_id` (e.g. the incident was resolved)."""
        self._last_sent.pop(incident_id, None)

    def __len__(self) -> int:
        return len(self._last_sent)


__all__ = ["DedupTracker"]
