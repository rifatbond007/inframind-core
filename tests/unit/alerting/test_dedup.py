"""Tests for `alerting.dedup.DedupTracker` — one alert per open incident.

The tracker is process-local (the durable mirror is the `alerts` table). It uses an
injectable clock so tests don't sleep. All timings are deterministic.
"""

from __future__ import annotations

from inframind.alerting.dedup import DedupTracker


class FakeClock:
    """Manually-advanced monotonic clock for deterministic window tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tracker(window: float = 300.0) -> tuple[DedupTracker, FakeClock]:
    clock = FakeClock()
    return DedupTracker(window_s=window, clock=clock), clock


def test_first_alert_for_unknown_incident_is_allowed() -> None:
    tracker, _ = _tracker()
    assert tracker.should_alert("inc_1", "warning") is True


def test_second_alert_within_window_is_suppressed() -> None:
    tracker, clock = _tracker(window=300.0)
    assert tracker.should_alert("inc_1", "warning") is True
    tracker.record("inc_1", "warning")

    clock.advance(10.0)
    assert tracker.should_alert("inc_1", "warning") is False


def test_alert_rearms_after_window_elapses() -> None:
    tracker, clock = _tracker(window=300.0)
    tracker.record("inc_1", "warning")

    clock.advance(299.0)
    assert tracker.should_alert("inc_1", "warning") is False

    clock.advance(2.0)  # total 301s
    assert tracker.should_alert("inc_1", "warning") is True


def test_severity_escalation_rearms_within_window() -> None:
    """A `critical` escalation fires even inside the dedup window."""
    tracker, clock = _tracker(window=300.0)
    tracker.record("inc_1", "warning")

    clock.advance(10.0)
    assert tracker.should_alert("inc_1", "critical") is True


def test_severity_demotion_does_not_rearm() -> None:
    """`warning` after `critical` within the window is suppressed."""
    tracker, clock = _tracker(window=300.0)
    tracker.record("inc_1", "critical")

    clock.advance(10.0)
    assert tracker.should_alert("inc_1", "warning") is False


def test_same_severity_does_not_rearm() -> None:
    """Escalation is one-directional; the same level is still deduped."""
    tracker, clock = _tracker(window=300.0)
    tracker.record("inc_1", "critical")

    clock.advance(10.0)
    assert tracker.should_alert("inc_1", "critical") is False


def test_forget_drops_state() -> None:
    """After `forget`, the next alert for that incident fires immediately."""
    tracker, _clock = _tracker(window=300.0)
    tracker.record("inc_1", "warning")

    tracker.forget("inc_1")
    assert tracker.should_alert("inc_1", "warning") is True


def test_different_incidents_do_not_share_dedup_state() -> None:
    tracker, _ = _tracker()
    assert tracker.should_alert("inc_1", "warning") is True
    tracker.record("inc_1", "warning")

    assert tracker.should_alert("inc_2", "warning") is True  # unaffected


def test_len_reflects_tracked_incidents() -> None:
    tracker, _ = _tracker()
    assert len(tracker) == 0
    tracker.record("inc_1", "warning")
    tracker.record("inc_2", "warning")
    assert len(tracker) == 2
    tracker.forget("inc_1")
    assert len(tracker) == 1
