"""Baseline freeze coordination (D4): one lease per service, fanned out to every detector.

An incident opening on a service freezes that service's baselines in all detectors so the
fault is not learned as normal. The freeze is a *renewable lease* of `lease_s` seconds
(`BASELINE_FREEZE_WINDOW`): the incident tracker renews it on every anomaly, so a live
incident stays frozen indefinitely, and it only expires on its own if the tracker disappears
or goes quiet without resolving (standard 05 §5.6 safety net). Time is the signals' own
timestamps, so replays behave like live runs (D6).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from .base import Detector


class BaselineFreezeCoordinator:
    """Per-service freeze leases applied to a fixed set of detectors."""

    def __init__(self, detectors: Sequence[Detector], lease_s: float = 300.0) -> None:
        self._detectors = list(detectors)
        self._lease = timedelta(seconds=lease_s)
        self._deadlines: dict[str, datetime] = {}

    def freeze(self, service: str, ts: datetime) -> None:
        """Freeze `service` and (re)set its lease to end `lease_s` after `ts`."""
        self._deadlines[service] = ts + self._lease
        for detector in self._detectors:
            detector.freeze(service, ts)

    def unfreeze(self, service: str) -> None:
        """Release `service`'s lease and unfreeze every detector; a no-op if not frozen."""
        if self._deadlines.pop(service, None) is None:
            return
        for detector in self._detectors:
            detector.unfreeze(service)

    def expire(self, now: datetime) -> set[str]:
        """Unfreeze and return every service whose lease ended at or before `now`."""
        expired = {service for service, end in self._deadlines.items() if end <= now}
        for service in expired:
            self.unfreeze(service)
        return expired

    def is_frozen(self, service: str) -> bool:
        """Return whether `service` currently holds a freeze lease."""
        return service in self._deadlines
