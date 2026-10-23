"""Building blocks every detector shares: the idempotence guard and the frozen-service set."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from inframind.detection.base import FrozenServices, LastSeen

T = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def test_last_seen_accepts_only_newer_timestamps_per_key() -> None:
    seen = LastSeen()
    assert seen.is_new(("frontend", "cpu"), T)
    assert not seen.is_new(("frontend", "cpu"), T)
    assert not seen.is_new(("frontend", "cpu"), T - timedelta(seconds=30))
    assert seen.is_new(("frontend", "cpu"), T + timedelta(seconds=30))


def test_last_seen_keys_are_independent() -> None:
    seen = LastSeen()
    assert seen.is_new(("frontend", "cpu"), T)
    assert seen.is_new(("frontend", "memory"), T)
    assert seen.is_new(("cartservice", "cpu"), T)


def test_frozen_services_toggle_per_service() -> None:
    frozen = FrozenServices()
    assert not frozen.is_frozen("frontend")
    frozen.freeze("frontend")
    assert frozen.is_frozen("frontend")
    assert not frozen.is_frozen("cartservice")
    frozen.unfreeze("frontend")
    assert not frozen.is_frozen("frontend")
    frozen.unfreeze("frontend")  # no-op
