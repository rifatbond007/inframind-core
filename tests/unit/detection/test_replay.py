"""Replay regression guard: recorded real signals through the default engine."""

from __future__ import annotations

from pathlib import Path

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Signal
from inframind.detection.engine import DetectionEngine

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "quiet_cluster_signals.ndjson"


def load() -> list[Signal]:
    lines = FIXTURE.read_text().splitlines()
    return sorted((Signal.model_validate_json(line) for line in lines), key=lambda s: s.ts)


def replay(signals: list[Signal]) -> list[Anomaly]:
    engine = DetectionEngine.default({})
    return [a for s in signals for a in engine.process(s)]


def test_the_fixture_is_real_and_small() -> None:
    signals = load()
    assert len(signals) > 3000
    assert FIXTURE.stat().st_size < 5 * 1024 * 1024
    assert {s.service for s in signals} >= {"frontend", "cartservice", "productcatalogservice"}
    assert {s.attrs["metric"] for s in signals} == {"cpu_cores", "memory_working_set_bytes"}


def metric_only(anomalies: list[Anomaly]) -> list[Anomaly]:
    """Drop `pod_replaced`: the recording has real frontend pod replacements (see below)."""
    return [a for a in anomalies if a.detector != "pod_replaced"]


def test_quiet_cluster_produces_no_metric_anomalies() -> None:
    assert metric_only(replay(load())) == []


def test_the_real_frontend_pod_replacements_are_the_only_pod_anomalies() -> None:
    """The recording caught three frontend pod-kills (smoke C6); nothing else was replaced.

    Pod `bmdg5` was replaced by `s9bgh`, then `hr269`, then `vnl8x` (20:08-20:14 UTC). The
    earlier `jfpxt` was seen once before a collection gap and is relearned, not reported.
    """
    found = [a for a in replay(load()) if a.detector == "pod_replaced"]
    assert [(a.service, a.attrs["pod"][-5:]) for a in found] == [
        ("frontend", "s9bgh"),
        ("frontend", "hr269"),
        ("frontend", "vnl8x"),
    ]


def test_an_injected_spike_in_real_data_is_found() -> None:
    signals = load()
    cpu = [
        i
        for i, s in enumerate(signals)
        if s.service == "frontend" and s.attrs["metric"] == "cpu_cores"
    ]
    victim = signals[cpu[len(cpu) // 2]]  # well past warm-up
    spiked = victim.model_copy(
        update={"attrs": {**victim.attrs, "value": victim.attrs["value"] * 50 + 0.5}}
    )
    signals[cpu[len(cpu) // 2]] = spiked
    anomalies = metric_only(replay(signals))
    assert anomalies, "the injected spike was not detected"
    assert {a.signal_id for a in anomalies} == {spiked.id}
    assert {a.detector for a in anomalies} >= {"zscore", "ewma"}


def test_replay_is_deterministic() -> None:
    signals = load()
    signals[1500] = signals[1500].model_copy(
        update={"attrs": {**signals[1500].attrs, "value": signals[1500].attrs["value"] * 40 + 1.0}}
    )

    def fingerprint(found: list[Anomaly]) -> list[tuple[str, str, float]]:
        return [(a.detector, a.signal_id, round(a.score, 9)) for a in found]

    assert fingerprint(replay(signals)) == fingerprint(replay(signals))
