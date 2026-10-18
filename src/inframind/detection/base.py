"""Building blocks every detector shares (standard 05 §5.1-5.3)."""

from __future__ import annotations

from collections.abc import Hashable
from datetime import datetime
from typing import Protocol

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Signal


class Detector(Protocol):
    """A long-lived, per-service stateful detector.

    A frozen service keeps being *scored* against its frozen baseline, so a sustained fault
    keeps firing; it just stops *learning* (D4).
    """

    name: str

    def update(self, signal: Signal) -> Anomaly | None:
        """Score one signal; return an anomaly when the detector fires."""

    def freeze(self, service: str, ts: datetime) -> None:
        """Stop updating `service`'s baseline (an incident opened)."""

    def unfreeze(self, service: str) -> None:
        """Resume updating `service`'s baseline."""

    def is_frozen(self, service: str) -> bool:
        """Return whether `service`'s baseline is frozen."""


class FrozenServices:
    """The set of services whose baselines are frozen."""

    def __init__(self) -> None:
        self._frozen: set[str] = set()

    def freeze(self, service: str) -> None:
        """Mark `service` frozen."""
        self._frozen.add(service)

    def unfreeze(self, service: str) -> None:
        """Mark `service` unfrozen; a no-op when it was not frozen."""
        self._frozen.discard(service)

    def is_frozen(self, service: str) -> bool:
        """Return whether `service` is frozen."""
        return service in self._frozen


class LastSeen:
    """Remembers the newest timestamp per key so a redelivered sample is processed once."""

    def __init__(self) -> None:
        self._last: dict[Hashable, datetime] = {}

    def is_new(self, key: Hashable, ts: datetime) -> bool:
        """Return True and remember `ts` when it is newer than the last one for `key`."""
        last = self._last.get(key)
        if last is not None and ts <= last:
            return False
        self._last[key] = ts
        return True
