"""Error-count spike detector: bursts, baselines, mixed sources, freeze and redelivery."""

from __future__ import annotations

from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.detection.error_spike import ErrorSpikeDetector

from .helpers import jaeger_error, loki_error, metric_signal, sec, span_latency


def fired(results: list) -> list:
    return [r for r in results if r is not None]


def test_third_error_in_a_window_fires() -> None:
    detector = ErrorSpikeDetector()
    first, second, third = (detector.update(loki_error(t)) for t in (0, 20, 40))
    assert first is None and second is None
    assert third is not None
    assert third.detector == "error_spike"
    assert third.score == 3.0
    assert third.severity is Severity.WARNING
    assert third.attrs["count"] == 3 and third.attrs["baseline"] == 0.0
    assert third.attrs["source"] == "loki"


def test_six_errors_within_a_minute_is_critical() -> None:
    detector = ErrorSpikeDetector()
    results = [detector.update(loki_error(t)) for t in (0, 10, 20, 30, 40, 50)]
    assert results[-1] is not None and results[-1].severity is Severity.CRITICAL
    assert results[2] is not None and results[2].severity is Severity.WARNING


def test_errors_spread_over_the_window_do_not_fire() -> None:
    detector = ErrorSpikeDetector()
    assert fired([detector.update(loki_error(t)) for t in (0, 200, 400)]) == []


def test_jaeger_error_spans_and_loki_errors_count_together() -> None:
    detector = ErrorSpikeDetector()
    results = [
        detector.update(loki_error(0)),
        detector.update(jaeger_error(10)),
        detector.update(loki_error(20, level="warning")),
    ]
    assert results[:2] == [None, None]
    assert results[2] is not None


def test_other_signals_are_ignored() -> None:
    detector = ErrorSpikeDetector()
    others = [
        span_latency(500.0, 0),
        metric_signal(9.0, 1),
        Signal(
            ts=sec(2),
            source=Source.ALERTMANAGER,
            type=SignalType.ALERT,
            service="frontend",
            attrs={"alertname": "X"},
        ),
    ]
    assert [detector.update(s) for s in others] == [None, None, None]


def test_a_noisy_baseline_raises_the_bar() -> None:
    detector = ErrorSpikeDetector()
    steady = [detector.update(loki_error(t)) for t in range(0, 3901, 150)]
    assert fired(steady) == []  # about two errors per window all hour: never alarming
    burst = [detector.update(loki_error(4000 + k)) for k in range(8)]
    assert fired(burst[:3]) == []  # 3 events is not enough above a baseline of 2 per window
    assert burst[3] is not None  # 2 prior + 4 burst = 6 >= max(3, 3 * 2.0)
    assert burst[3].attrs["baseline"] == 2.0


def test_events_during_a_freeze_do_not_enter_the_baseline() -> None:
    detector = ErrorSpikeDetector()
    detector.freeze("frontend", sec(0))
    during = [detector.update(loki_error(t)) for t in range(0, 1000, 25)]
    assert fired(during) != []  # the fault keeps firing while frozen
    detector.unfreeze("frontend")
    after = [detector.update(loki_error(t)) for t in (1400, 1420, 1440)]
    assert after[2] is not None
    assert after[2].attrs["baseline"] == 0.0


def test_redelivered_signal_is_ignored() -> None:
    detector = ErrorSpikeDetector()
    e0, e20, e40 = loki_error(0), loki_error(20), loki_error(40)
    results = [detector.update(s) for s in (e0, e20, e20, e40)]
    assert results[:3] == [None, None, None]
    assert results[3] is not None and results[3].attrs["count"] == 3


def test_separate_services_have_separate_counts() -> None:
    detector = ErrorSpikeDetector()
    results = [
        detector.update(loki_error(0, service="frontend")),
        detector.update(loki_error(10, service="cartservice")),
        detector.update(loki_error(20, service="frontend")),
        detector.update(loki_error(30, service="cartservice")),
    ]
    assert fired(results) == []
