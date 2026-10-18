"""IncidentTracker: open, update, resolve, and the freeze it drives (D4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.correlation.config import CorrelationParams
from inframind.correlation.fingerprint import fingerprint
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


PARAM_FIELDS = {
    "window_s",
    "cooldown_s",
    "max_age_s",
    "update_interval_s",
    "critical_services",
    "critical_anomalies",
    "enabled",
}


def make(**kw: Any) -> tuple[IncidentTracker, BaselineFreezeCoordinator, Recording]:
    """Build a tracker over a recording detector; CorrelationParams fields and tracker caps mix."""
    detector = Recording()
    coordinator = BaselineFreezeCoordinator([detector], lease_s=300.0)
    params = CorrelationParams(**{k: v for k, v in kw.items() if k in PARAM_FIELDS})
    caps = {k: v for k, v in kw.items() if k not in PARAM_FIELDS}
    return IncidentTracker(coordinator, params, **caps), coordinator, detector


def alert(service: str, signal_id: str) -> Signal:
    return Signal(
        id=signal_id,
        ts=s(0),
        source=Source.ALERTMANAGER,
        type=SignalType.ALERT,
        service=service,
        attrs={"alertname": "KubePodCrashLooping"},
    )


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
    assert tracker.on_anomaly(anomaly(10)) == []  # inside the update interval: folded in silently
    assert tracker.on_anomaly(anomaly(20, detector="ewma")) == []
    [resolved] = tracker.tick(s(20 + 120))
    assert len(resolved.anomalies) == 3
    assert resolved.title == "frontend: zscore anomaly"  # the opening detector names it


def test_a_severity_increase_republishes_with_a_new_revision() -> None:
    tracker, _, _ = make()
    tracker.on_anomaly(anomaly(0, severity=Severity.WARNING))
    [updated] = tracker.on_anomaly(anomaly(30, severity=Severity.CRITICAL))
    assert updated.revision == 2 and updated.state == "open"
    assert updated.severity is Severity.CRITICAL
    assert updated.updated_at == s(30) and updated.opened_at == s(0)
    later = tracker.on_anomaly(anomaly(70, severity=Severity.WARNING))
    assert all(i.severity is Severity.CRITICAL for i in later)  # a later snapshot never lowers it


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


def test_services_beyond_the_window_are_independent() -> None:
    tracker, coordinator, _ = make(cooldown_s=120.0, window_s=50.0)
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
    before = tracker.tick(s(100 + 119))
    assert all(i.state == "open" for i in before)  # evidence may be published; nothing resolves
    after = tracker.tick(s(100 + 120))
    assert [i.state for i in after] == ["resolved"]


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


SIX = [
    "adservice",
    "cartservice",
    "currencyservice",
    "frontend",
    "productcatalogservice",
    "recommendationservice",
]


def test_six_services_firing_together_become_one_incident() -> None:
    """The motivating case: one burst on the frontend lifted CPU on six services."""
    tracker, coordinator, _ = make()
    published: list = []
    for k, service in enumerate(SIX):
        published += tracker.on_anomaly(anomaly(k * 5, service=service))
    assert len({i.id for i in published}) == 1
    assert len({i.fingerprint for i in published}) == 1
    assert [i.revision for i in published] == [1, 2, 3, 4, 5, 6]  # every new service is news
    assert published[-1].services == SIX
    assert tracker.open_services == frozenset(SIX)
    assert all(coordinator.is_frozen(service) for service in SIX)


def test_anomalies_outside_the_window_open_a_separate_incident() -> None:
    far, _, _ = make(window_s=300.0)
    far.on_anomaly(anomaly(0, service="frontend"))
    [second] = far.on_anomaly(anomaly(301, service="cartservice"))
    assert second.services == ["cartservice"]
    near, _, _ = make(window_s=300.0)
    near.on_anomaly(anomaly(0, service="frontend"))
    joined = near.on_anomaly(anomaly(300, service="cartservice"))
    assert joined and joined[0].services == ["cartservice", "frontend"]


def test_the_same_service_rejoins_its_open_incident_beyond_the_window() -> None:
    tracker, _, _ = make(window_s=300.0)
    [opened] = tracker.on_anomaly(anomaly(0))
    again = tracker.on_anomaly(anomaly(5000))
    assert all(i.id == opened.id for i in again)


def test_the_fingerprint_is_fixed_at_open_and_survives_joins() -> None:
    tracker, _, _ = make()
    [opened] = tracker.on_anomaly(anomaly(0, service="frontend"))
    [joined] = tracker.on_anomaly(anomaly(5, service="cartservice"))
    assert joined.fingerprint == opened.fingerprint
    assert opened.fingerprint == fingerprint("frontend", s(0), 300.0)


def test_a_flap_inside_the_bucket_reuses_the_fingerprint() -> None:
    """Review Focus 1: resolve, then fire again in the same window: new incident, same key."""
    tracker, _, _ = make(cooldown_s=60.0)
    [first] = tracker.on_anomaly(anomaly(10, service="frontend"))
    tracker.tick(s(10 + 60))
    [second] = tracker.on_anomaly(anomaly(100, service="frontend"))
    assert second.id != first.id and second.revision == 1
    assert second.fingerprint == first.fingerprint


def test_severity_is_critical_for_three_services_twenty_anomalies_or_any_critical() -> None:
    three, _, _ = make()
    published = [
        i
        for k, svc in enumerate(["a", "b", "c"])
        for i in three.on_anomaly(anomaly(k, service=svc))
    ]
    assert [i.severity for i in published] == [
        Severity.WARNING,
        Severity.WARNING,
        Severity.CRITICAL,
    ]

    two, _, _ = make()
    snapshots = [
        i for k in range(5) for i in two.on_anomaly(anomaly(k, service="a" if k % 2 else "b"))
    ]
    assert all(i.severity is Severity.WARNING for i in snapshots)

    many, _, _ = make(update_interval_s=1000.0)
    snapshots = [i for k in range(20) for i in many.on_anomaly(anomaly(k))]
    assert snapshots[-1].severity is Severity.CRITICAL  # the 20th anomaly crosses the threshold

    one, _, _ = make()
    [opened] = one.on_anomaly(anomaly(0, severity=Severity.CRITICAL))
    assert opened.severity is Severity.CRITICAL


def test_a_flood_publishes_a_handful_of_snapshots() -> None:
    """Review Focus 4: 900 per-event anomalies must not become 900 snapshots."""
    tracker, _, _ = make(update_interval_s=30.0)
    published = [
        i for k in range(900) for i in tracker.on_anomaly(anomaly(k, detector="error_spike"))
    ]
    published += tracker.tick(s(900 + 120))
    assert 2 <= len(published) <= 1 + 900 // 30 + 3
    assert published[-1].state == "resolved"


def test_quiet_services_stay_frozen_while_the_incident_is_open() -> None:
    """Review Focus 3: cartservice joined at 10 s and never fires again; it must stay frozen."""
    tracker, coordinator, _ = make(cooldown_s=1000.0, max_age_s=100_000.0)
    tracker.on_anomaly(anomaly(0, service="frontend"))
    tracker.on_anomaly(anomaly(10, service="cartservice"))
    for t in range(100, 1001, 100):
        tracker.on_anomaly(anomaly(t, service="frontend"))
        assert coordinator.expire(s(t)) == set(), f"a lease expired at +{t}"
    assert coordinator.is_frozen("cartservice")


def test_ticks_keep_every_service_frozen_without_new_anomalies() -> None:
    tracker, coordinator, _ = make(cooldown_s=2000.0, max_age_s=100_000.0)
    tracker.on_anomaly(anomaly(0, service="frontend"))
    tracker.on_anomaly(anomaly(10, service="cartservice"))
    for t in range(100, 1001, 100):
        tracker.tick(s(t))
        assert coordinator.expire(s(t)) == set(), f"a lease expired at +{t}"


def test_an_incident_expires_at_max_age_and_releases_every_baseline() -> None:
    tracker, coordinator, detector = make(max_age_s=3600.0, cooldown_s=1000.0)
    first_id = ""
    expired = []
    ids: set[str] = set()
    for t in range(0, 4001, 100):
        for incident in [*tracker.on_anomaly(anomaly(t, service="frontend")), *tracker.tick(s(t))]:
            first_id = first_id or incident.id
            ids.add(incident.id)
            if incident.state == "expired":
                expired.append(incident)
                assert not coordinator.is_frozen("frontend")
    [one] = expired
    assert one.id == first_id and one.closed_at == s(3600)
    assert detector.calls.count(("unfreeze", "frontend")) >= 1
    # the fault persisted: the anomaly right after the expiry opened a NEW incident (new id)
    assert len(ids) == 2 and first_id in ids


def test_acknowledge_keeps_baselines_frozen_and_still_resolves() -> None:
    tracker, coordinator, _ = make(cooldown_s=120.0)
    [opened] = tracker.on_anomaly(anomaly(0))
    [acked] = tracker.acknowledge(opened.id, s(10))
    assert acked.state == "acknowledged" and acked.revision == 2 and acked.closed_at is None
    assert coordinator.is_frozen("frontend")
    assert tracker.acknowledge(opened.id, s(20)) == []  # already acknowledged
    assert tracker.acknowledge("no-such-id", s(20)) == []
    [resolved] = tracker.tick(s(121))
    assert resolved.state == "resolved" and resolved.revision == 3
    assert tracker.acknowledge(opened.id, s(130)) == []  # terminal


def test_attach_signal_adds_an_alert_id_to_an_open_incident_only() -> None:
    tracker, _, _ = make(cooldown_s=60.0)
    tracker.attach_signal(alert("frontend", "alert-early"))  # nothing open: ignored, no error
    tracker.on_anomaly(anomaly(0, service="frontend"))
    tracker.attach_signal(alert("frontend", "alert-1"))
    tracker.attach_signal(alert("cartservice", "alert-other"))  # not in the incident
    [resolved] = tracker.tick(s(60))
    assert "alert-1" in resolved.signals
    assert "alert-early" not in resolved.signals and "alert-other" not in resolved.signals


def test_a_late_anomaly_never_lowers_opened_at_or_reopens_a_closed_incident() -> None:
    """Review Focus 5."""
    tracker, _, _ = make(cooldown_s=120.0)
    tracker.on_anomaly(anomaly(100))
    tracker.on_anomaly(anomaly(40))  # delivered late
    [resolved] = tracker.tick(s(100 + 120))
    assert resolved.opened_at == s(100)
    [late] = tracker.on_anomaly(anomaly(50))  # for a service whose incident already closed
    assert late.id != resolved.id and late.revision == 1


def test_correlation_disabled_gives_one_incident_per_anomaly() -> None:
    tracker, _, _ = make(enabled=False)
    published = [
        i
        for k, svc in enumerate(["a", "b", "c"])
        for i in tracker.on_anomaly(anomaly(k, service=svc))
    ]
    assert len({i.id for i in published}) == 3
    assert all(i.services in (["a"], ["b"], ["c"]) for i in published)
