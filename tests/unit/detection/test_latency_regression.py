"""p95 latency regression: persistence, span-count and absolute-delta guards, freeze, redelivery."""

from __future__ import annotations

from inframind.common.signal import Severity
from inframind.detection.latency_regression import LatencyRegressionDetector

from .helpers import jaeger_error, metric_signal, span_latency


def feed(
    detector: LatencyRegressionDetector,
    values: list[float],
    *,
    start: int = 0,
    span_count: int = 40,
) -> list:
    return [
        detector.update(span_latency(v, start + k, span_count=span_count))
        for k, v in enumerate(values)
    ]


def fired(results: list) -> list:
    return [r for r in results if r is not None]


def test_two_consecutive_breaches_fire() -> None:
    detector = LatencyRegressionDetector()
    assert fired(feed(detector, [16.0] * 15)) == []
    first = detector.update(span_latency(60.0, 15))
    second = detector.update(span_latency(60.0, 16))
    assert first is None
    assert second is not None
    assert second.detector == "latency_regression"
    assert abs(second.score - 3.75) < 1e-9
    assert second.severity is Severity.CRITICAL
    assert second.attrs["median_ms"] == 16.0 and second.attrs["p95_ms"] == 60.0
    assert second.attrs["consecutive"] == 2 and second.attrs["frozen"] is False


def test_a_single_isolated_spike_does_not_fire() -> None:
    values = [16.0] * 14 + [77.0] + [16.0] * 10 + [77.0] + [16.0] * 5
    assert fired(feed(LatencyRegressionDetector(), values)) == []


def test_polls_with_few_spans_are_ignored_and_not_learned() -> None:
    detector = LatencyRegressionDetector()
    feed(detector, [16.0] * 15)
    assert fired(feed(detector, [5000.0] * 5, start=15, span_count=2)) == []
    detector.update(span_latency(60.0, 20))
    anomaly = detector.update(span_latency(60.0, 21))
    assert anomaly is not None and anomaly.attrs["median_ms"] == 16.0


def test_small_absolute_regressions_do_not_fire() -> None:
    results = feed(LatencyRegressionDetector(), [0.07] * 15 + [0.2] * 5)  # 2.9x but +0.13 ms
    assert fired(results) == []


def test_warmup_never_fires() -> None:
    assert fired(feed(LatencyRegressionDetector(), [16.0] * 8 + [500.0, 500.0])) == []


def test_redelivered_signal_is_ignored() -> None:
    detector = LatencyRegressionDetector()
    feed(detector, [16.0] * 15)
    breach = span_latency(60.0, 15)
    assert detector.update(breach) is None
    assert detector.update(breach) is None  # redelivery must not count as a second breach
    assert detector.update(span_latency(60.0, 16)) is not None


def test_error_spans_and_other_metrics_are_ignored() -> None:
    detector = LatencyRegressionDetector()
    assert detector.update(jaeger_error(0)) is None
    assert detector.update(metric_signal(9.0, 1)) is None


def test_services_are_independent() -> None:
    detector = LatencyRegressionDetector()
    for k in range(15):
        detector.update(span_latency(16.0, k))
        detector.update(span_latency(2.0, k, service="recommendationservice"))
    detector.update(span_latency(60.0, 15))
    # 60 ms is a regression for the frontend but the other service has no breach history
    assert detector.update(span_latency(2.1, 15, service="recommendationservice")) is None


def test_frozen_series_keeps_scoring_but_does_not_learn() -> None:
    detector = LatencyRegressionDetector()
    feed(detector, [16.0] * 15)
    detector.freeze("frontend", span_latency(0, 0).ts)
    results = feed(detector, [60.0] * 10, start=15)
    assert results[0] is None
    assert all(r is not None for r in results[1:])
    assert all(r.attrs["median_ms"] == 16.0 and r.attrs["frozen"] for r in results[1:])
    detector.unfreeze("frontend")
    feed(detector, [60.0] * 25, start=25)  # learned: 60 ms becomes the new normal
    detector.update(span_latency(200.0, 50))
    probe = detector.update(span_latency(200.0, 51))
    assert probe is not None and probe.attrs["median_ms"] == 60.0 and not probe.attrs["frozen"]
