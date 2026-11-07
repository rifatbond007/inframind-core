"""p95 latency regression detector for traces (standard 05, D3: statistical only).

Consumes the per-service `span_latency` signals the Jaeger collector emits (one per poll,
carrying the p95 of server spans and the span count). The baseline is the rolling *median* of
recent p95 values, which a single outlier cannot move. Real Boutique traffic is bursty: its
frontend p95 spikes in isolation about one poll in six, so a breach must persist for
`consecutive` polls before it fires (16% of polls fire on one breach, 2% on two, on recorded
data). Polls with too few spans are noise and are neither scored nor learned. A frozen
service keeps being scored while its history stops growing (D4).
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Severity, Signal, SignalType, Source

from .base import FrozenServices, LastSeen
from .config import LatencyParams

_MIN_MEDIAN_MS = 0.001


@dataclass
class _Service:
    history: deque[float]
    run: int = 0


class LatencyRegressionDetector:
    """Per-service p95 regression against a rolling-median baseline."""

    name = "latency_regression"

    def __init__(self, params: LatencyParams | None = None) -> None:
        self._params = params or LatencyParams()
        self._services: dict[str, _Service] = {}
        self._frozen = FrozenServices()
        self._seen = LastSeen()

    def update(self, signal: Signal) -> Anomaly | None:
        """Score one latency poll; learn from it unless its service is frozen."""
        if signal.source is not Source.JAEGER or signal.type is not SignalType.TRACE:
            return None
        attrs = signal.attrs
        if attrs.get("metric") != "span_latency":
            return None
        p95, spans = attrs.get("p95_ms"), attrs.get("span_count")
        if not isinstance(p95, int | float) or not isinstance(spans, int | float):
            return None
        if not self._seen.is_new(signal.service, signal.ts) or spans < self._params.min_spans:
            return None
        state = self._services.setdefault(
            signal.service, _Service(history=deque(maxlen=self._params.window))
        )
        anomaly = self._score(signal, float(p95), int(spans), state)
        if not self._frozen.is_frozen(signal.service):
            state.history.append(float(p95))
        return anomaly

    def _score(self, signal: Signal, p95: float, spans: int, state: _Service) -> Anomaly | None:
        p = self._params
        if len(state.history) < p.min_samples:
            state.run = 0
            return None
        median = statistics.median(state.history)
        breach = p95 > median * (1 + p.regression_pct / 100) and p95 - median >= p.min_delta_ms
        state.run = state.run + 1 if breach else 0
        if state.run < p.consecutive:
            return None
        ratio = p95 / max(median, _MIN_MEDIAN_MS)
        return Anomaly(
            ts=signal.ts,
            detector=self.name,
            service=signal.service,
            signal_id=signal.id,
            score=ratio,
            severity=Severity.CRITICAL if ratio >= 3 else Severity.WARNING,
            attrs={
                "p95_ms": p95,
                "median_ms": median,
                "span_count": spans,
                "consecutive": state.run,
                "regression_pct": p.regression_pct,
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
