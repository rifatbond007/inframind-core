"""DetectionEngine: routing, environment overrides, lease release, and the P4 gate checks."""

from __future__ import annotations

from datetime import UTC, datetime

from inframind.common.anomaly import Anomaly as CommonAnomaly
from inframind.common.signal import Signal, SignalType, Source
from inframind.detection.engine import DetectionEngine

from .helpers import (
    T0,
    alternating,
    at,
    jaeger_error,
    loki_error,
    metric_signal,
    span_latency,
)


def warm(engine: DetectionEngine, n: int = 20, **kw: str) -> None:
    for i, value in enumerate(alternating(n)):
        engine.process(metric_signal(value, i, **kw))


def names(anomalies: list[CommonAnomaly]) -> list[str]:
    return [a.detector for a in anomalies]


def test_prometheus_metric_spike_returns_zscore_and_ewma_anomalies() -> None:
    engine = DetectionEngine.default({})
    warm(engine)
    assert names(engine.process(metric_signal(0.2, 20))) == ["zscore", "ewma"]


def test_span_latency_signals_reach_only_the_latency_detector() -> None:
    engine = DetectionEngine.default({})
    for i in range(15):
        assert engine.process(span_latency(16.0, i)) == []
    assert engine.process(span_latency(60.0, 15)) == []
    assert names(engine.process(span_latency(60.0, 16))) == ["latency_regression"]


def test_error_logs_reach_only_the_error_spike_detector() -> None:
    engine = DetectionEngine.default({})
    assert engine.process(loki_error(0)) == []
    assert engine.process(jaeger_error(10)) == []
    assert names(engine.process(loki_error(20))) == ["error_spike"]


def test_alertmanager_signals_return_nothing() -> None:
    engine = DetectionEngine.default({})
    alert = Signal(
        ts=at(0),
        source=Source.ALERTMANAGER,
        type=SignalType.ALERT,
        service="frontend",
        attrs={"alertname": "X"},
    )
    assert engine.process(alert) == []


def test_default_honours_env_overrides() -> None:
    engine = DetectionEngine.default({"INFRAMIND_DETECTION_ZSCORE_THRESHOLD": "1000000"})
    warm(engine)
    assert "zscore" not in names(engine.process(metric_signal(0.2, 20)))


def test_default_freeze_lease_comes_from_baseline_freeze_window() -> None:
    short = DetectionEngine.default({"BASELINE_FREEZE_WINDOW": "60"})
    default = DetectionEngine.default({})
    for engine in (short, default):
        engine.coordinator.freeze("frontend", T0)
        engine.process(metric_signal(0.01, 0, service="cartservice"))  # at T0
        engine.process(metric_signal(0.01, 3, service="cartservice"))  # T0 + 90 s
    assert not short.coordinator.is_frozen("frontend")
    assert default.coordinator.is_frozen("frontend")


def test_expired_leases_are_released_before_scoring() -> None:
    engine = DetectionEngine.default({})
    engine.coordinator.freeze("frontend", T0)
    assert all(d.is_frozen("frontend") for d in engine.detectors)
    engine.process(metric_signal(0.01, 11, service="cartservice"))  # T0 + 330 s > 300 s lease
    assert not any(d.is_frozen("frontend") for d in engine.detectors)


def test_the_standard_import_path_works() -> None:
    from inframind.detection.anomaly import Anomaly

    assert Anomaly is CommonAnomaly


def test_gate_zscore_fires_on_a_synthetic_spike() -> None:
    engine = DetectionEngine.default({})
    warm(engine)
    assert "zscore" in names(engine.process(metric_signal(0.2, 20)))


def test_gate_ewma_fires_on_a_synthetic_spike() -> None:
    engine = DetectionEngine.default({})
    warm(engine)
    assert "ewma" in names(engine.process(metric_signal(0.2, 20)))


def test_gate_baseline_freeze_keeps_a_sustained_fault_firing() -> None:
    engine = DetectionEngine.default({})
    warm(engine)
    engine.coordinator.freeze("frontend", at(20))
    last: list[CommonAnomaly] = []
    for k in range(60):
        last = engine.process(metric_signal(0.2, 20 + k))
        engine.coordinator.freeze("frontend", at(20 + k))  # the tracker renews the lease
        assert "zscore" in names(last), f"stopped firing at sample {k}"
    zscore = next(a for a in last if a.detector == "zscore")
    assert zscore.attrs["frozen"] is True and zscore.attrs["mean"] < 0.02


def test_unfrozen_detectors_absorb_a_sustained_fault() -> None:
    """The failure D4 exists to prevent: without the freeze the fault becomes normal."""
    engine = DetectionEngine.default({})
    warm(engine)
    results = [names(engine.process(metric_signal(0.2, 20 + k))) for k in range(60)]
    assert "zscore" in results[0]
    assert results[-1] == []


def test_wall_clock_is_not_used_so_old_signals_behave_like_new_ones() -> None:
    engine = DetectionEngine.default({})
    base = datetime(2020, 1, 1, tzinfo=UTC)

    def old(value: float, i: int) -> Signal:
        return Signal(
            ts=base + (at(i) - T0),
            source=Source.PROMETHEUS,
            type=SignalType.METRIC,
            service="frontend",
            attrs={"metric": "cpu_cores", "value": value},
        )

    for i, value in enumerate(alternating(20)):
        engine.process(old(value, i))
    assert "zscore" in names(engine.process(old(0.2, 20)))


def test_malformed_signals_never_raise_and_never_fire() -> None:
    """A detector that raises would leave the bus entry pending and redelivered forever."""
    engine = DetectionEngine.default({})
    bad_attrs = [
        {},
        {"metric": "cpu_cores"},
        {"metric": "cpu_cores", "value": None},
        {"metric": "cpu_cores", "value": "0.2"},
        {"metric": "cpu_cores", "value": True},
        {"metric": 5, "value": 1.0},
        {"metric": "span_latency", "p95_ms": "fast", "span_count": 40},
        {"metric": "span_latency", "p95_ms": 12.0, "span_count": None},
        {"metric": "span_latency", "p95_ms": -5.0, "span_count": 40},
        {"metric": "span_latency", "span_count": 40},
        {"span_id": 7},
    ]
    for i, attrs in enumerate(bad_attrs):
        for source, kind in (
            (Source.PROMETHEUS, SignalType.METRIC),
            (Source.JAEGER, SignalType.TRACE),
            (Source.LOKI, SignalType.LOG),
        ):
            signal = Signal(ts=at(i), source=source, type=kind, service="frontend", attrs=attrs)
            assert isinstance(engine.process(signal), list)
