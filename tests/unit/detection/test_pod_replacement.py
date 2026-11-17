"""PodReplacementDetector: a new pod for an established service fires once (D29)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from inframind.common.signal import Signal, SignalType, Source
from inframind.detection.config import PodReplacementParams, load_params
from inframind.detection.engine import DetectionEngine
from inframind.detection.pod_replacement import PodReplacementDetector

T0 = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)


def metric(pod: str, at_s: float, service: str = "frontend") -> Signal:
    return Signal(
        ts=T0 + timedelta(seconds=at_s),
        source=Source.PROMETHEUS,
        type=SignalType.METRIC,
        service=service,
        attrs={"metric": "cpu_cores", "value": 0.02, "labels": {"pod": pod}},
    )


def run(detector: PodReplacementDetector, signals: list[Signal]) -> list[tuple[float, str]]:
    out = []
    for s in signals:
        a = detector.update(s)
        if a is not None:
            out.append(((a.ts - T0).total_seconds(), a.attrs["pod"]))
    return out


def steady(pod: str, start: float, end: float, service: str = "frontend") -> list[Signal]:
    return [metric(pod, t, service) for t in range(int(start), int(end), 30)]


def test_a_replacement_after_warmup_fires_once() -> None:
    d = PodReplacementDetector()
    signals = steady("frontend-a", 0, 600) + steady("frontend-b", 600, 900)
    assert run(d, signals) == [(600.0, "frontend-b")]


def test_the_anomaly_names_the_new_and_the_previous_pod() -> None:
    d = PodReplacementDetector()
    run(d, steady("frontend-a", 0, 600))
    anomaly = d.update(metric("frontend-b", 600))
    assert anomaly is not None
    assert anomaly.detector == "pod_replaced"
    assert anomaly.service == "frontend"
    assert anomaly.attrs["previous_pods"] == ["frontend-a"]
    assert anomaly.score == 5.0


def test_pods_seen_during_warmup_are_learned_not_reported() -> None:
    """A detection-worker restart sees every pod as new; none of them is a replacement."""
    d = PodReplacementDetector()
    assert run(d, steady("frontend-a", 0, 120) + steady("frontend-b", 120, 280)) == []


def test_a_redelivered_signal_of_a_known_pod_is_a_no_op() -> None:
    d = PodReplacementDetector()
    run(d, steady("frontend-a", 0, 600))
    replaced = metric("frontend-b", 600)
    assert d.update(replaced) is not None
    assert d.update(replaced) is None


def test_a_service_back_after_a_collection_gap_is_relearned() -> None:
    """Every pod expired (no data for longer than pod_ttl_s): no predecessor, no anomaly."""
    d = PodReplacementDetector()
    signals = steady("frontend-a", 0, 600) + steady("frontend-b", 600 + 1800 + 60, 2600)
    assert run(d, signals) == []


def test_services_are_independent() -> None:
    d = PodReplacementDetector()
    signals = sorted(
        steady("frontend-a", 0, 900)
        + steady("cart-a", 0, 600, "cartservice")
        + steady("cart-b", 600, 900, "cartservice"),
        key=lambda s: s.ts,
    )
    found = [(a.service, a.attrs["pod"]) for s in signals if (a := d.update(s))]
    assert found == [("cartservice", "cart-b")]


def test_non_prometheus_signals_and_pod_less_metrics_are_ignored() -> None:
    d = PodReplacementDetector()
    log = Signal(
        ts=T0, source=Source.LOKI, type=SignalType.LOG, service="frontend", attrs={"pod": "x"}
    )
    bare = Signal(
        ts=T0,
        source=Source.PROMETHEUS,
        type=SignalType.METRIC,
        service="frontend",
        attrs={"metric": "cpu_cores", "value": 1.0},
    )
    assert d.update(log) is None
    assert d.update(bare) is None


def test_freezing_does_not_change_scoring() -> None:
    d = PodReplacementDetector()
    run(d, steady("frontend-a", 0, 600))
    d.freeze("frontend", T0)
    anomaly = d.update(metric("frontend-b", 600))
    assert anomaly is not None and anomaly.attrs["frozen"] is True


def test_the_engine_runs_it_and_env_overrides_apply() -> None:
    engine = DetectionEngine.default({})
    anomalies = [a for s in steady("frontend-a", 0, 600) for a in engine.process(s)]
    anomalies += engine.process(metric("frontend-b", 600))
    assert [a.detector for a in anomalies] == ["pod_replaced"]
    env = {"INFRAMIND_DETECTION_POD_REPLACED_WARMUP_S": "60"}
    assert load_params(PodReplacementParams, "pod_replaced", env).warmup_s == 60.0
