"""Z-score detector: spikes, floors, counters, redelivery and the freeze discipline (D4)."""

from __future__ import annotations

from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.detection.zscore import ZScoreDetector

from .helpers import alternating, at, feed, metric_signal


def fired(results: list) -> list:
    return [r for r in results if r is not None]


def test_spike_after_warmup_fires() -> None:
    detector = ZScoreDetector()
    assert fired(feed(detector, alternating(20))) == []
    spike = metric_signal(0.2, 20)
    anomaly = detector.update(spike)
    assert anomaly is not None
    assert anomaly.detector == "zscore"
    assert anomaly.service == "frontend"
    assert anomaly.signal_id == spike.id
    assert anomaly.ts == spike.ts
    assert anomaly.score > 3
    assert anomaly.severity is Severity.CRITICAL
    assert anomaly.attrs["frozen"] is False
    assert abs(anomaly.attrs["mean"] - 0.012) < 1e-9
    assert anomaly.attrs["metric"] == "cpu_cores"
    assert anomaly.attrs["window"] == 20


def test_quiet_series_never_fires() -> None:
    assert fired(feed(ZScoreDetector(), alternating(60))) == []


def test_warmup_never_fires() -> None:
    results = feed(ZScoreDetector(), [0.01] * 8 + [10.0])
    assert fired(results) == []


def test_flat_series_with_a_tiny_wiggle_does_not_fire() -> None:
    results = feed(ZScoreDetector(), [0.0] * 20 + [0.004])
    assert fired(results) == []


def test_warning_vs_critical_severity() -> None:
    warm = [0.0, 0.02] * 10  # mean 0.01, std 0.01
    d1 = ZScoreDetector()
    feed(d1, warm)
    a1 = d1.update(metric_signal(0.045, 20))  # z = 3.5
    assert a1 is not None and a1.severity is Severity.WARNING
    d2 = ZScoreDetector()
    feed(d2, warm)
    a2 = d2.update(metric_signal(0.06, 20))  # z = 5.0
    assert a2 is not None and a2.severity is Severity.CRITICAL


def test_counter_delta_fires_on_first_restart() -> None:
    detector = ZScoreDetector()
    results = feed(detector, [0.0] * 15 + [1.0], metric="container_restarts_total")
    assert fired(results[:-1]) == []
    anomaly = results[-1]
    assert anomaly is not None
    assert anomaly.score == 4.0
    assert anomaly.severity is Severity.CRITICAL


def test_counter_reset_is_not_an_anomaly() -> None:
    detector = ZScoreDetector()
    totals = [0.0] * 12 + [3.0] * 4 + [0.0] * 4  # a restart storm, then the pod is replaced
    results = feed(detector, totals, metric="container_restarts_total")
    assert results[12] is not None  # the jump from 0 to 3 is a real signal
    assert fired(results[13:]) == []  # the drop to 0 is a reset, not a negative delta


def test_redelivered_signal_is_ignored() -> None:
    detector = ZScoreDetector()
    feed(detector, alternating(20))
    spike = metric_signal(0.2, 20)
    assert detector.update(spike) is not None
    assert detector.update(spike) is None
    follow_up = detector.update(metric_signal(0.2, 21))
    assert follow_up is not None
    assert follow_up.attrs["window"] == 21  # the redelivery was not learned twice


def test_metrics_of_one_service_are_independent() -> None:
    detector = ZScoreDetector()
    feed(detector, alternating(20, 60e6, 64e6), metric="memory_working_set_bytes")
    feed(detector, alternating(20))
    detector.update(metric_signal(0.2, 20))
    memory_spike = detector.update(metric_signal(400e6, 20, metric="memory_working_set_bytes"))
    assert memory_spike is not None
    assert 5e7 < memory_spike.attrs["mean"] < 7e7


def test_non_metric_and_non_prometheus_signals_are_ignored() -> None:
    detector = ZScoreDetector()
    log = Signal(
        ts=at(0),
        source=Source.LOKI,
        type=SignalType.LOG,
        service="frontend",
        attrs={"level": "error"},
    )
    other = Signal(
        ts=at(1),
        source=Source.JAEGER,
        type=SignalType.METRIC,
        service="frontend",
        attrs={"metric": "cpu_cores", "value": 9.0},
    )
    assert detector.update(log) is None
    assert detector.update(other) is None


def test_frozen_series_keeps_scoring_but_does_not_learn() -> None:
    detector = ZScoreDetector()
    feed(detector, alternating(20))
    detector.freeze("frontend", at(20))
    assert detector.is_frozen("frontend")
    results = feed(detector, [0.2] * 30, start=20)
    assert all(r is not None for r in results)
    assert all(r.attrs["frozen"] is True for r in results if r is not None)
    last = results[-1]
    assert last is not None and last.attrs["mean"] < 0.02
    detector.unfreeze("frontend")
    # Unfrozen, the same level is learned (and stops firing): that is the absorption D4 prevents.
    feed(detector, [0.2] * 5, start=50)
    probe = detector.update(metric_signal(5.0, 55))
    assert probe is not None
    assert probe.attrs["frozen"] is False
    assert probe.attrs["mean"] > 0.03  # the baseline moved while unfrozen


def test_freezing_one_service_leaves_others_learning() -> None:
    detector = ZScoreDetector()
    feed(detector, alternating(20))
    feed(detector, alternating(20), service="cartservice")
    detector.freeze("frontend", at(20))
    other = feed(detector, [0.2] * 3, service="cartservice", start=20)
    assert other[-1] is not None and other[-1].attrs["frozen"] is False


def test_a_drop_in_a_resource_gauge_is_not_an_anomaly() -> None:
    """A service going idle (traffic stopped) is not a fault; resource gauges alert upward only."""
    detector = ZScoreDetector()
    busy = alternating(20, 0.10, 0.14)
    assert fired(feed(detector, [*busy, 0.0, 0.0, 0.0])) == []
    memory = alternating(20, 60e6, 64e6)
    assert fired(feed(detector, [*memory, 5e6], metric="memory_working_set_bytes")) == []


def test_an_unknown_metric_fires_in_both_directions() -> None:
    detector = ZScoreDetector()
    feed(detector, alternating(20, 100.0, 104.0), metric="queue_depth")
    drop = detector.update(metric_signal(0.0, 20, metric="queue_depth"))
    assert drop is not None and drop.score < 0
