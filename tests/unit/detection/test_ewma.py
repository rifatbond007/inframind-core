"""EWMA detector: level shifts, floors, counters, redelivery and the freeze discipline (D4)."""

from __future__ import annotations

from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.detection.ewma import EwmaDetector

from .helpers import alternating, at, feed, metric_signal


def fired(results: list) -> list:
    return [r for r in results if r is not None]


def test_step_change_fires() -> None:
    detector = EwmaDetector()
    assert fired(feed(detector, alternating(20))) == []
    spike = metric_signal(0.2, 20)
    anomaly = detector.update(spike)
    assert anomaly is not None
    assert anomaly.detector == "ewma"
    assert anomaly.signal_id == spike.id
    assert anomaly.ts == spike.ts
    assert anomaly.score > 3
    assert anomaly.severity is Severity.CRITICAL
    assert anomaly.attrs["frozen"] is False
    assert anomaly.attrs["alpha"] == 0.3
    assert 0.011 < anomaly.attrs["mean"] < 0.013


def test_quiet_series_never_fires() -> None:
    assert fired(feed(EwmaDetector(), alternating(60))) == []


def test_warmup_never_fires() -> None:
    assert fired(feed(EwmaDetector(), [0.01] * 8 + [10.0])) == []


def test_flat_series_with_a_tiny_wiggle_does_not_fire() -> None:
    assert fired(feed(EwmaDetector(), [0.0] * 20 + [0.004])) == []


def test_counter_delta_fires_on_first_restart() -> None:
    results = feed(EwmaDetector(), [0.0] * 15 + [1.0], metric="container_restarts_total")
    assert fired(results[:-1]) == []
    anomaly = results[-1]
    assert anomaly is not None
    assert anomaly.score == 4.0
    assert anomaly.severity is Severity.CRITICAL


def test_counter_reset_is_not_an_anomaly() -> None:
    totals = [0.0] * 12 + [3.0] * 4 + [0.0] * 4
    results = feed(EwmaDetector(), totals, metric="container_restarts_total")
    assert results[12] is not None
    assert fired(results[13:]) == []


def test_redelivered_signal_is_ignored() -> None:
    detector = EwmaDetector()
    feed(detector, alternating(20))
    first = detector.update(metric_signal(0.2, 20))
    assert first is not None
    assert detector.update(metric_signal(0.2, 20)) is None
    follow_up = detector.update(metric_signal(5.0, 21))  # clearly above the learned level
    assert follow_up is not None
    learned_once = first.attrs["mean"] + 0.3 * (0.2 - first.attrs["mean"])
    assert abs(follow_up.attrs["mean"] - learned_once) < 1e-9  # the redelivery was not learned


def test_non_metric_and_non_prometheus_signals_are_ignored() -> None:
    detector = EwmaDetector()
    log = Signal(ts=at(0), source=Source.LOKI, type=SignalType.LOG, service="frontend")
    assert detector.update(log) is None


def test_frozen_series_keeps_scoring_but_does_not_learn() -> None:
    detector = EwmaDetector()
    feed(detector, alternating(20))
    detector.freeze("frontend", at(20))
    results = feed(detector, [0.2] * 30, start=20)
    assert all(r is not None for r in results)
    assert all(r.attrs["frozen"] is True for r in results if r is not None)
    last = results[-1]
    assert last is not None and last.attrs["mean"] < 0.02
    detector.unfreeze("frontend")
    feed(detector, [0.2] * 5, start=50)  # learned within a few samples: no longer anomalous
    probe = detector.update(metric_signal(5.0, 55))
    assert probe is not None and probe.attrs["frozen"] is False
    assert probe.attrs["mean"] > 0.1


def test_it_is_not_a_slow_ramp_detector() -> None:
    ramp = [0.012 + 0.0015 * k for k in range(26)]  # 0.012 -> 0.0495
    assert fired(feed(EwmaDetector(), alternating(20) + ramp)) == []


def test_a_drop_in_a_resource_gauge_is_not_an_anomaly() -> None:
    """A service going idle (traffic stopped) is not a fault; resource gauges alert upward only."""
    detector = EwmaDetector()
    busy = alternating(20, 0.10, 0.14)
    assert fired(feed(detector, [*busy, 0.0, 0.0, 0.0])) == []
    memory = alternating(20, 60e6, 64e6)
    assert fired(feed(detector, [*memory, 5e6], metric="memory_working_set_bytes")) == []


def test_an_unknown_metric_fires_in_both_directions() -> None:
    detector = EwmaDetector()
    feed(detector, alternating(20, 100.0, 104.0), metric="queue_depth")
    drop = detector.update(metric_signal(0.0, 20, metric="queue_depth"))
    assert drop is not None and drop.score < 0
