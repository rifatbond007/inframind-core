"""Rolling Z-score detector for metrics (standard 05, D3: statistical only).

Per series `(service, metric)` it keeps the last `window` values. Once `min_samples` are held
it scores each new value as `|x - mean| / std`, where `std` is floored by the metric's profile
so an idle series cannot make a tiny wiggle look enormous. Counters are scored on their
per-sample increase. A frozen service keeps being scored but its window stops growing (D4).
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Severity, Signal

from .base import CounterDeltas, FrozenServices, LastSeen, effective_std, metric_point
from .config import ZScoreParams, profile_for


class ZScoreDetector:
    """Per-service rolling Z-score on Prometheus metric samples."""

    name = "zscore"

    def __init__(self, params: ZScoreParams | None = None) -> None:
        self._params = params or ZScoreParams()
        self._windows: dict[tuple[str, str], deque[float]] = {}
        self._frozen = FrozenServices()
        self._seen = LastSeen()
        self._counters = CounterDeltas()

    def update(self, signal: Signal) -> Anomaly | None:
        """Score one signal; learn from it unless its service is frozen."""
        point = metric_point(signal)
        if point is None:
            return None
        metric, value = point
        key = (signal.service, metric)
        if not self._seen.is_new(key, signal.ts):
            return None
        profile = profile_for(metric)
        if profile.counter:
            delta = self._counters.delta(key, value)
            if delta is None:
                return None
            value = delta
        window = self._windows.setdefault(key, deque(maxlen=self._params.window))
        anomaly = self._score(signal, metric, value, window)
        if not self._frozen.is_frozen(signal.service):
            window.append(value)
        return anomaly

    def _score(
        self, signal: Signal, metric: str, value: float, window: deque[float]
    ) -> Anomaly | None:
        if len(window) < self._params.min_samples:
            return None
        mean = sum(window) / len(window)
        std = math.sqrt(sum((x - mean) ** 2 for x in window) / len(window))
        profile = profile_for(metric)
        divisor = effective_std(std, mean, profile)
        z = (value - mean) / divisor
        if abs(z) < self._params.threshold or (profile.direction == "up" and z < 0):
            return None
        critical = profile.counter or abs(z) >= 1.5 * self._params.threshold
        return Anomaly(
            ts=signal.ts,
            detector=self.name,
            service=signal.service,
            signal_id=signal.id,
            score=z,
            severity=Severity.CRITICAL if critical else Severity.WARNING,
            attrs={
                "metric": metric,
                "value": value,
                "mean": mean,
                "std": divisor,
                "window": len(window),
                "threshold": self._params.threshold,
                "frozen": self._frozen.is_frozen(signal.service),
            },
        )

    def freeze(self, service: str, ts: datetime) -> None:
        """Stop learning `service`'s baseline; scoring continues."""
        self._frozen.freeze(service)

    def unfreeze(self, service: str) -> None:
        """Resume learning `service`'s baseline."""
        self._frozen.unfreeze(service)

    def is_frozen(self, service: str) -> bool:
        """Return whether `service`'s baseline is frozen."""
        return self._frozen.is_frozen(service)
