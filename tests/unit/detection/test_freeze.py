"""BaselineFreezeCoordinator: fan-out to detectors and a renewable freeze lease (D4)."""

from __future__ import annotations

from datetime import datetime, timedelta

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Signal
from inframind.detection.freeze import BaselineFreezeCoordinator

from .helpers import T0


class Recording:
    """A fake detector that records the lifecycle calls it receives."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._frozen: set[str] = set()

    def update(self, signal: Signal) -> Anomaly | None:
        return None

    def freeze(self, service: str, ts: datetime) -> None:
        self.calls.append(("freeze", service, ts))
        self._frozen.add(service)

    def unfreeze(self, service: str) -> None:
        self.calls.append(("unfreeze", service))
        self._frozen.discard(service)

    def is_frozen(self, service: str) -> bool:
        return service in self._frozen


def s(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_freeze_fans_out_to_every_detector() -> None:
    a, b = Recording(), Recording()
    coordinator = BaselineFreezeCoordinator([a, b])
    coordinator.freeze("frontend", s(0))
    assert a.calls == [("freeze", "frontend", s(0))]
    assert b.calls == [("freeze", "frontend", s(0))]
    assert coordinator.is_frozen("frontend") and not coordinator.is_frozen("cartservice")


def test_lease_expires_after_lease_s_and_unfreezes_every_detector_once() -> None:
    a, b = Recording(), Recording()
    coordinator = BaselineFreezeCoordinator([a, b], lease_s=300.0)
    coordinator.freeze("frontend", s(0))
    assert coordinator.expire(s(299)) == set()
    assert coordinator.expire(s(301)) == {"frontend"}
    assert a.calls[-1] == ("unfreeze", "frontend") and b.calls[-1] == ("unfreeze", "frontend")
    assert [c[0] for c in a.calls].count("unfreeze") == 1
    assert not coordinator.is_frozen("frontend")
    assert coordinator.expire(s(10_000)) == set()


def test_renewal_extends_the_lease_for_a_sustained_fault() -> None:
    coordinator = BaselineFreezeCoordinator([Recording()], lease_s=300.0)
    coordinator.freeze("frontend", s(0))
    for t in range(100, 1001, 100):  # a fault that keeps firing for 1000 s
        coordinator.freeze("frontend", s(t))
        assert coordinator.expire(s(t + 1)) == set()
        assert coordinator.is_frozen("frontend")
    assert coordinator.expire(s(1301)) == {"frontend"}


def test_explicit_unfreeze_clears_the_lease() -> None:
    detector = Recording()
    coordinator = BaselineFreezeCoordinator([detector], lease_s=300.0)
    coordinator.freeze("frontend", s(0))
    coordinator.unfreeze("frontend")
    assert detector.calls[-1] == ("unfreeze", "frontend")
    assert coordinator.expire(s(10_000)) == set()
    assert not coordinator.is_frozen("frontend")


def test_unfreezing_an_unfrozen_service_calls_no_detector() -> None:
    detector = Recording()
    BaselineFreezeCoordinator([detector]).unfreeze("frontend")
    assert detector.calls == []


def test_services_have_independent_leases() -> None:
    coordinator = BaselineFreezeCoordinator([Recording()], lease_s=300.0)
    coordinator.freeze("frontend", s(0))
    coordinator.freeze("cartservice", s(200))
    assert coordinator.expire(s(301)) == {"frontend"}
    assert coordinator.is_frozen("cartservice")
    assert coordinator.expire(s(501)) == {"cartservice"}
