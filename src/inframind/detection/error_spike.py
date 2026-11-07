"""Error-count spike detector for logs and error spans (standard 05, D3).

An *error event* is a Loki warning/error log line or a Jaeger error span. The collectors
publish only problem lines, never totals, so this is a count spike, not an error *ratio*
(design ruling 10). Per service it counts events in a sliding `window_s` and compares with a
baseline: the average events per window over the preceding `baseline_windows` windows,
counting only events seen while the service was not frozen, so a long fault cannot raise its
own bar (D4). A frozen service keeps being scored.
"""

from __future__ import annotations

from bisect import bisect_right, insort
from dataclasses import dataclass, field
from datetime import datetime

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Severity, Signal, SignalType, Source

from .base import FrozenServices
from .config import ErrorSpikeParams

_MAX_SEEN_IDS = 20_000


def is_error_event(signal: Signal) -> bool:
    """Return whether `signal` is a Loki problem line or a Jaeger error span."""
    if signal.source is Source.LOKI and signal.type is SignalType.LOG:
        return True
    return (
        signal.source is Source.JAEGER
        and signal.type is SignalType.TRACE
        and "span_id" in signal.attrs
    )


@dataclass
class _Events:
    all_ts: list[float] = field(default_factory=list)  # every event, sorted
    baseline_ts: list[float] = field(default_factory=list)  # events seen while unfrozen, sorted


class ErrorSpikeDetector:
    """Per-service sliding-window error-event count against a rolling baseline."""

    name = "error_spike"

    def __init__(self, params: ErrorSpikeParams | None = None) -> None:
        self._params = params or ErrorSpikeParams()
        self._events: dict[str, _Events] = {}
        self._frozen = FrozenServices()
        self._seen_ids: dict[str, None] = {}

    def update(self, signal: Signal) -> Anomaly | None:
        """Record one error event and fire when the window count spikes above baseline."""
        if not is_error_event(signal) or not self._first_time(signal.id):
            return None
        p = self._params
        t = signal.ts.timestamp()
        events = self._events.setdefault(signal.service, _Events())
        insort(events.all_ts, t)
        if not self._frozen.is_frozen(signal.service):
            insort(events.baseline_ts, t)
        horizon = t - (p.baseline_windows + 1) * p.window_s
        for series in (events.all_ts, events.baseline_ts):
            del series[: bisect_right(series, horizon)]

        count = bisect_right(events.all_ts, t) - bisect_right(events.all_ts, t - p.window_s)
        in_baseline = bisect_right(events.baseline_ts, t - p.window_s) - bisect_right(
            events.baseline_ts, t - (p.baseline_windows + 1) * p.window_s
        )
        baseline = in_baseline / p.baseline_windows
        if count < max(p.min_count, p.k * baseline):
            return None
        return Anomaly(
            ts=signal.ts,
            detector=self.name,
            service=signal.service,
            signal_id=signal.id,
            score=count / max(baseline, 1.0),
            severity=Severity.CRITICAL if count >= 2 * p.min_count else Severity.WARNING,
            attrs={
                "count": count,
                "baseline": baseline,
                "window_s": p.window_s,
                "min_count": p.min_count,
                "k": p.k,
                "frozen": self._frozen.is_frozen(signal.service),
                "source": signal.source.value,
            },
        )

    def _first_time(self, signal_id: str) -> bool:
        if signal_id in self._seen_ids:
            return False
        self._seen_ids[signal_id] = None
        while len(self._seen_ids) > _MAX_SEEN_IDS:
            del self._seen_ids[next(iter(self._seen_ids))]
        return True

    def freeze(self, service: str, ts: datetime) -> None:
        """Stop counting new events toward `service`'s baseline; scoring continues."""
        self._frozen.freeze(service)

    def unfreeze(self, service: str) -> None:
        """Resume counting events toward `service`'s baseline."""
        self._frozen.unfreeze(service)

    def is_frozen(self, service: str) -> bool:
        """Return whether `service`'s baseline is frozen."""
        return self._frozen.is_frozen(service)
