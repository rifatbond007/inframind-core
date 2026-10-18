"""Exponentially weighted moving average detector for metrics (standard 05, D3).

Per series `(service, metric)` it keeps an exponentially weighted mean and variance. A sample
fires when its residual from the mean exceeds `sigma_k` standard deviations (floored by the
metric's profile). It has no window edge and follows recent level shifts, which complements
the windowed Z-score; with the default `alpha` its memory is only a few samples, so it is not a
slow-ramp detector. A frozen service keeps being scored but its mean and variance stop moving.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Severity, Signal

from .base import CounterDeltas, FrozenServices, LastSeen, effective_std, metric_point
from .config import EwmaParams, profile_for


@dataclass
class _Series:
    mean: float = 0.0
    var: float = 0.0
    n: int = 0


class EwmaDetector:
    """Per-service EWMA residual detector on Prometheus metric samples."""

    name = "ewma"

    def __init__(self, params: EwmaParams | None = None) -> None:
        self._params = params or EwmaParams()
        self._series: dict[tuple[str, str], _Series] = {}
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
        state = self._series.get(key)
        if state is None:
            self._series[key] = _Series(mean=value, var=0.0, n=1)
            return None
        anomaly = self._score(signal, metric, value, state)
        if not self._frozen.is_frozen(signal.service):
            residual = value - state.mean
            state.mean += self._params.alpha * residual
            state.var = (1 - self._params.alpha) * (state.var + self._params.alpha * residual**2)
            state.n += 1
        return anomaly

    def _score(self, signal: Signal, metric: str, value: float, state: _Series) -> Anomaly | None:
        if state.n < self._params.min_samples:
            return None
        profile = profile_for(metric)
        std = effective_std(math.sqrt(state.var), state.mean, profile)
        residual = value - state.mean
        score = residual / std
        if abs(score) <= self._params.sigma_k:
            return None
        critical = profile.counter or abs(score) >= 1.5 * self._params.sigma_k
        return Anomaly(
            ts=signal.ts,
            detector=self.name,
            service=signal.service,
            signal_id=signal.id,
            score=score,
            severity=Severity.CRITICAL if critical else Severity.WARNING,
            attrs={
                "metric": metric,
                "value": value,
                "mean": state.mean,
                "std": std,
                "alpha": self._params.alpha,
                "sigma_k": self._params.sigma_k,
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
