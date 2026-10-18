"""IncidentTracker: open, update, resolve, and the freeze it drives (D4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Severity, Signal
from inframind.correlation.tracker import IncidentTracker
from inframind.detection.freeze import BaselineFreezeCoordinator

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def s(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class Recording:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def update(self, signal: Signal) -> Anomaly | None:
        return None

    def freeze(self, service: str, ts: datetime) -> None:
        self.calls.append(("freeze", service))

    def unfreeze(self, service: str) -> None:
        self.calls.append(("unfreeze", service))

    def is_frozen(self, service: str) -> bool:
        return False


def anomaly(
    seconds: float,
    *,
    service: str = "frontend",
    severity: Severity = Severity.WARNING,
    detector: str = "zscore",
    signal_id: str | None = None,
) -> Anomaly:
    return Anomaly(
        ts=s(seconds),
        detector=detector,
        service=service,
        signal_id=signal_id or f"sig-{service}-{seconds}",
        score=4.0,
        severity=severity,
    )


def make(**kw: Any) -> tuple[IncidentTracker, BaselineFreezeCoordinator, Recording]:
    detector = Recording()
    coordinator = BaselineFreezeCoordinator([detector], lease_s=300.0)
    return IncidentTracker(coordinator, **kw), coordinator, detector


def test_first_anomaly_opens_one_incident_and_freezes() -> None:
    tracker, coordinator, detector = make()
    a = anomaly(0)
    [incident] = tracker.on_anomaly(a)
    assert incident.state == "open" and incident.revision == 1
    assert incident.services == ["frontend"]
    assert incident.title == "frontend: zscore anomaly"
    assert incident.severity is Severity.WARNING
    assert incident.opened_at == a.ts == incident.updated_at
    assert incident.closed_at is None
    assert incident.signals == [a.signal_id] and incident.anomalies == [a]
    assert ("freeze", "frontend") in detector.calls
    assert coordinator.is_frozen("frontend")
    assert tracker.open_services == frozenset({"frontend"})


def test_further_anomalies_update_without_republishing() -> None:
    tracker, _, _ = make()
    tracker.on_anomaly(anomaly(0))
    assert tracker.on_anomaly(anomaly(30)) == []
    assert tracker.on_anomaly(anomaly(60, detector="ewma")) == []
    [resolved] = tracker.tick(s(60 + 120))
    assert len(resolved.anomalies) == 3
    assert resolved.title == "frontend: zscore anomaly"  # the opening detector names it


def test_a_severity_increase_republishes_with_a_new_revision() -> None:
    tracker, _, _ = make()
    tracker.on_anomaly(anomaly(0, severity=Severity.WARNING))
    [updated] = tracker.on_anomaly(anomaly(30, severity=Severity.CRITICAL))
    assert updated.revision == 2 and updated.state == "open"
    assert updated.severity is Severity.CRITICAL
    assert updated.updated_at == s(30) and updated.opened_at == s(0)
    assert tracker.on_anomaly(anomaly(60, severity=Severity.WARNING)) == []  # never lowered


def test_quiet_cooldown_resolves_and_unfreezes() -> None:
    tracker, coordinator, detector = make(cooldown_s=120.0)
    [opened] = tracker.on_anomaly(anomaly(0))
    assert tracker.tick(s(119)) == []
    [resolved] = tracker.tick(s(120))
    assert resolved.id == opened.id
    assert resolved.state == "resolved" and resolved.revision == 2
    assert resolved.closed_at == s(120) and resolved.updated_at == s(120)
    assert detector.calls[-1] == ("unfreeze", "frontend")
    assert not coordinator.is_frozen("frontend")
    assert tracker.open_services == frozenset()
    assert tracker.tick(s(1000)) == []


def test_anomalies_renew_the_freeze_lease() -> None:
    tracker, coordinator, _ = make(cooldown_s=120.0)
    for t in range(0, 1001, 100):  # a fault that keeps firing for ~17 minutes
        tracker.on_anomaly(anomaly(t))
        assert coordinator.expire(s(t + 250)) == set(), f"lease expired at +{t}"
    assert tracker.tick(s(1000 + 119)) == []
    assert len(tracker.tick(s(1000 + 120))) == 1


def test_services_are_independent() -> None:
    tracker, coordinator, _ = make(cooldown_s=120.0)
    tracker.on_anomaly(anomaly(0, service="frontend"))
    tracker.on_anomaly(anomaly(100, service="cartservice"))
    [resolved] = tracker.tick(s(121))
    assert resolved.services == ["frontend"]
    assert tracker.open_services == frozenset({"cartservice"})
    assert coordinator.is_frozen("cartservice") and not coordinator.is_frozen("frontend")


def test_a_service_can_reopen_after_resolving() -> None:
    tracker, _, _ = make(cooldown_s=60.0)
    [first] = tracker.on_anomaly(anomaly(0))
    tracker.tick(s(61))
    [second] = tracker.on_anomaly(anomaly(100))
    assert second.id != first.id and second.revision == 1 and second.state == "open"


def test_caps_keep_the_newest_anomalies_and_signal_ids() -> None:
    tracker, _, _ = make(max_anomalies=3, max_signals=3, cooldown_s=60.0)
    for k in range(5):
        tracker.on_anomaly(anomaly(k * 10))
    [resolved] = tracker.tick(s(40 + 60))
    assert [a.ts for a in resolved.anomalies] == [s(20), s(30), s(40)]
    assert resolved.signals == ["sig-frontend-20", "sig-frontend-30", "sig-frontend-40"]


def test_out_of_order_anomalies_do_not_move_the_quiet_clock_backwards() -> None:
    tracker, _, _ = make(cooldown_s=120.0)
    tracker.on_anomaly(anomaly(100))
    tracker.on_anomaly(anomaly(40))  # a late delivery
    assert tracker.tick(s(100 + 119)) == []
    assert len(tracker.tick(s(100 + 120))) == 1


def test_a_flood_from_one_detector_does_not_evict_the_evidence_of_the_others() -> None:
    """A burst of 900 error events must not push the metric anomaly that opened the incident out."""
    tracker, _, _ = make(cooldown_s=120.0)
    tracker.on_anomaly(anomaly(0, detector="zscore"))
    for k in range(1, 900):
        tracker.on_anomaly(anomaly(k, detector="error_spike"))
    tracker.on_anomaly(anomaly(901, detector="ewma"))
    [resolved] = tracker.tick(s(901 + 120))
    kept = [a.detector for a in resolved.anomalies]
    assert kept.count("zscore") == 1 and kept.count("ewma") == 1
    assert kept.count("error_spike") == 10  # the newest ten of the flood
    assert len(kept) == 12
    assert [a.ts for a in resolved.anomalies] == sorted(a.ts for a in resolved.anomalies)


def test_the_per_detector_cap_is_configurable() -> None:
    tracker, _, _ = make(cooldown_s=60.0, max_per_detector=2)
    for k in range(6):
        tracker.on_anomaly(anomaly(k * 10, detector="error_spike"))
    [resolved] = tracker.tick(s(50 + 60))
    assert [a.ts for a in resolved.anomalies] == [s(40), s(50)]
