"""The pure evaluation half of testbed/smoke_detection.py (the P4 gate); no cluster needed."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from inframind.common.anomaly import Anomaly
from inframind.common.incident import Incident
from inframind.common.signal import Severity

SCRIPT = Path(__file__).resolve().parents[2] / "testbed" / "smoke_detection.py"
spec = importlib.util.spec_from_file_location("smoke_detection", SCRIPT)
assert spec and spec.loader
smoke = importlib.util.module_from_spec(spec)
sys.modules["smoke_detection"] = smoke
spec.loader.exec_module(smoke)

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
BURST_START, BURST_END, COOLDOWN = T0, T0 + timedelta(seconds=45), 45.0


def s(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def anomaly(seconds: float, *, frozen: bool, mean: float, detector: str = "zscore") -> Anomaly:
    return Anomaly(
        ts=s(seconds),
        detector=detector,
        service="frontend",
        signal_id=f"sig-{seconds}",
        score=9.0,
        severity=Severity.CRITICAL,
        attrs={"frozen": frozen, "mean": mean, "metric": "cpu_cores"},
    )


def incident(
    state: str,
    revision: int,
    opened: float,
    *,
    closed: float | None = None,
    anomalies: list[Anomaly] | None = None,
    iid: str = "01INCIDENT",
    service: str = "frontend",
) -> Incident:
    return Incident(
        id=iid,
        revision=revision,
        state=state,
        opened_at=s(opened),
        updated_at=s(closed or opened),
        closed_at=s(closed) if closed is not None else None,
        title=f"{service}: zscore anomaly",
        services=[service],
        signals=[],
        anomalies=anomalies or [],
        severity=Severity.CRITICAL,
        fingerprint="0123456789abcdef",
    )


def healthy() -> list[Incident]:
    anomalies = [
        anomaly(20, frozen=False, mean=0.0003),
        anomaly(35, frozen=True, mean=0.0123),
        anomaly(50, frozen=True, mean=0.0123),
        anomaly(65, frozen=True, mean=0.0123),
    ]
    return [
        incident("open", 1, 20, anomalies=anomalies[:1]),
        incident("resolved", 2, 20, closed=150, anomalies=anomalies),
    ]


def by_name(results: list[Any]) -> dict[str, Any]:
    return {r.name: r for r in results}


def run(incidents: list[Incident]) -> dict[str, Any]:
    return by_name(smoke.evaluate(incidents, BURST_START, BURST_END, COOLDOWN))


def test_a_healthy_run_passes_every_check() -> None:
    results = smoke.evaluate(healthy(), BURST_START, BURST_END, COOLDOWN)
    assert [r.name for r in results] == ["quiet", "detected", "frozen", "resolved"]
    assert all(r.ok for r in results), [r for r in results if not r.ok]


def test_an_incident_before_the_burst_fails_quiet_only() -> None:
    early = incident("open", 1, -30, iid="01EARLY", service="cartservice")
    results = run([early, *healthy()])
    assert not results["quiet"].ok
    assert "cartservice" in results["quiet"].detail
    assert [n for n, r in results.items() if not r.ok] == ["quiet"]


def test_opening_more_than_60_seconds_after_the_burst_fails_detected() -> None:
    slow = [
        incident("open", 1, 61),
        incident("resolved", 2, 61, closed=200, anomalies=healthy()[1].anomalies),
    ]
    assert not run(slow)["detected"].ok
    assert run(healthy())["detected"].ok


def test_no_incident_for_the_target_fails_detected_and_everything_after() -> None:
    other = [incident("open", 1, 20, service="cartservice")]
    results = run(other)
    assert not results["detected"].ok
    assert not results["frozen"].ok and not results["resolved"].ok


def test_a_later_anomaly_that_was_not_frozen_fails_frozen() -> None:
    bad = healthy()
    anomalies = [*bad[1].anomalies[:2], anomaly(50, frozen=False, mean=0.0123), bad[1].anomalies[3]]
    bad[1] = incident("resolved", 2, 20, closed=150, anomalies=anomalies)
    assert not run(bad)["frozen"].ok


def test_a_drifting_baseline_while_frozen_fails_frozen() -> None:
    bad = healthy()
    anomalies = [
        bad[1].anomalies[0],
        anomaly(35, frozen=True, mean=0.0123),
        anomaly(50, frozen=True, mean=0.0456),
    ]
    bad[1] = incident("resolved", 2, 20, closed=150, anomalies=anomalies)
    assert not run(bad)["frozen"].ok


def test_fewer_than_two_frozen_anomalies_cannot_prove_the_freeze() -> None:
    thin = healthy()
    thin[1] = incident("resolved", 2, 20, closed=150, anomalies=thin[1].anomalies[:2])
    assert not run(thin)["frozen"].ok


def test_no_resolve_fails_resolved() -> None:
    assert not run(healthy()[:1])["resolved"].ok


def test_resolving_far_too_late_fails_resolved() -> None:
    late = healthy()
    late[1] = incident("resolved", 2, 20, closed=45 + 45 + 151, anomalies=late[1].anomalies)
    assert not run(late)["resolved"].ok


def test_describe_lists_every_snapshot_with_its_anomalies() -> None:
    lines = smoke.describe(healthy())
    text = "\n".join(lines)
    assert "open" in text and "resolved" in text
    assert "zscore" in text and "cpu_cores" in text
    assert len([line for line in lines if line.lstrip().startswith("incident")]) == 2


def test_describe_of_nothing_says_so() -> None:
    assert smoke.describe([]) == ["no incidents were published"]


def test_target_resolved_ignores_other_services_incidents() -> None:
    other = incident("resolved", 2, 20, closed=100, iid="01OTHER", service="adservice")
    assert not smoke.target_resolved([other, healthy()[0]], BURST_START)
    assert smoke.target_resolved([other, *healthy()], BURST_START)


def test_target_resolved_ignores_a_target_incident_from_before_the_burst() -> None:
    early = incident("resolved", 2, -100, closed=-10, iid="01EARLY")
    assert not smoke.target_resolved([early], BURST_START)
