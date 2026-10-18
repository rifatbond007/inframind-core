"""Signal builders shared by the detector tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Signal, SignalType, Source
from inframind.detection.base import Detector

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
STEP_S = 30


def at(i: int) -> datetime:
    """Return the timestamp of the i-th 30-second sample."""
    return T0 + timedelta(seconds=STEP_S * i)


def metric_signal(
    value: float, i: int, *, service: str = "frontend", metric: str = "cpu_cores"
) -> Signal:
    """A Prometheus metric signal as the collector publishes it."""
    return Signal(
        ts=at(i),
        source=Source.PROMETHEUS,
        type=SignalType.METRIC,
        service=service,
        attrs={"metric": metric, "value": value, "labels": {"pod": f"{service}-x"}},
    )


def alternating(n: int, lo: float = 0.010, hi: float = 0.014) -> list[float]:
    """n values alternating lo/hi, a quiet but not constant series."""
    return [lo if k % 2 == 0 else hi for k in range(n)]


def feed(
    detector: Detector,
    values: list[float],
    *,
    service: str = "frontend",
    metric: str = "cpu_cores",
    start: int = 0,
) -> list[Anomaly | None]:
    """Feed consecutive metric samples and return what the detector said for each."""
    return [
        detector.update(metric_signal(v, start + k, service=service, metric=metric))
        for k, v in enumerate(values)
    ]


def raw(**fields: Any) -> dict[str, Any]:
    """Return fields unchanged (documents intent at call sites)."""
    return fields
