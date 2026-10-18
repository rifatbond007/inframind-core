"""Incident tracker (P4b): group anomalies across services into one incident per fault.

An anomaly joins an open incident when its service is already in it or it lands within
`window_s` of the incident's last anomaly (`window.choose_incident`); otherwise it opens one.
Every service in an *active* incident (open or acknowledged) stays frozen (D4): the lease is
renewed on each anomaly and each tick, and released when the incident resolves (quiet for
`cooldown_s`) or expires (open for `max_age_s`). All times are signal time, so a replay behaves
like a live run (D6).

Publishing is rate-absorbing. A snapshot goes out on open, on a severity increase, when a new
service joins, on acknowledge/resolve/expire, and otherwise at most once per `update_interval_s`
when evidence changed. `revision` counts publishes. Retained anomalies are capped per detector,
then overall, so one noisy detector cannot evict the others' evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from inframind.common.anomaly import Anomaly
from inframind.common.ids import new_ulid
from inframind.common.incident import Incident, IncidentState
from inframind.common.signal import Severity, Signal
from inframind.detection.freeze import BaselineFreezeCoordinator

from .config import CorrelationParams
from .fingerprint import fingerprint
from .state_machine import is_active, transition
from .window import choose_incident

_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


@dataclass
class _Record:
    """The tracker's mutable view of one incident; snapshots are built from it."""

    id: str
    fingerprint: str
    first_service: str
    first_detector: str
    opened_at: datetime
    last_anomaly_ts: datetime
    severity: Severity
    state: IncidentState = "open"
    service_set: set[str] = field(default_factory=set)
    revision: int = 0
    anomaly_count: int = 0
    anomalies: list[Anomaly] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    last_published_at: datetime | None = None
    dirty: bool = False

    @property
    def services(self) -> frozenset[str]:
        """The services in the incident (the `window.Candidate` protocol)."""
        return frozenset(self.service_set)


class IncidentTracker:
    """Turns anomalies into incident snapshots and drives the baseline freeze."""

    def __init__(
        self,
        coordinator: BaselineFreezeCoordinator,
        params: CorrelationParams | None = None,
        *,
        cooldown_s: float | None = None,
        max_anomalies: int = 50,
        max_per_detector: int = 10,
        max_signals: int = 200,
    ) -> None:
        self._coordinator = coordinator
        self._params = params or CorrelationParams()
        if cooldown_s is not None:
            self._params = replace(self._params, cooldown_s=cooldown_s)
        self._max_anomalies = max_anomalies
        self._max_per_detector = max_per_detector
        self._max_signals = max_signals
        self._active: dict[str, _Record] = {}

    @property
    def open_services(self) -> frozenset[str]:
        """The services that belong to an active (open or acknowledged) incident."""
        return frozenset(s for record in self._active.values() for s in record.service_set)

    def on_anomaly(self, anomaly: Anomaly) -> list[Incident]:
        """Fold `anomaly` into an incident; return the snapshots to publish (zero or one)."""
        record = None
        if self._params.enabled:
            record = choose_incident(
                list(self._active.values()), anomaly.service, anomaly.ts, self._params.window_s
            )
        if record is None:
            record = self._open(anomaly)
            self._renew(record, anomaly.ts)
            return [self._snapshot(record, at=anomaly.ts)]

        new_service = anomaly.service not in record.service_set
        before = record.severity
        self._add(record, anomaly)
        record.severity = self._classify(record)
        self._renew(record, anomaly.ts)
        raised = _RANK[record.severity] > _RANK[before]
        if raised or new_service or self._due(record, anomaly.ts):
            return [self._snapshot(record, at=anomaly.ts)]
        return []

    def attach_signal(self, signal: Signal) -> None:
        """Attach an alert signal as corroboration to active incidents containing its service.

        It never opens an incident and never publishes; the id appears in the next snapshot.
        """
        for record in self._active.values():
            if signal.service in record.service_set:
                record.signals = [*record.signals, signal.id][-self._max_signals :]
                record.dirty = True

    def acknowledge(self, incident_id: str, at: datetime) -> list[Incident]:
        """Move an `open` incident to `acknowledged`; unknown or already-acknowledged: nothing."""
        record = self._active.get(incident_id)
        if record is None or record.state != "open":
            return []
        record.state = transition(record.state, "acknowledge")
        return [self._snapshot(record, at=at)]

    def tick(self, now: datetime) -> list[Incident]:
        """Expire or resolve incidents, keep the rest frozen; return the snapshots to publish."""
        out: list[Incident] = []
        for record in sorted(self._active.values(), key=lambda r: (r.opened_at, r.id)):
            if (now - record.opened_at).total_seconds() >= self._params.max_age_s:
                record.state = transition(record.state, "expire")
            elif (now - record.last_anomaly_ts).total_seconds() >= self._params.cooldown_s:
                record.state = transition(record.state, "resolve")
            else:
                self._renew(record, now)
                if self._due(record, now):
                    out.append(self._snapshot(record, at=now))
                continue
            out.append(self._snapshot(record, at=now))
            del self._active[record.id]
            self._release(record)
        return out

    # ------------------------------------------------------------------ internals

    def _open(self, anomaly: Anomaly) -> _Record:
        record = _Record(
            id=new_ulid(),
            fingerprint=fingerprint(anomaly.service, anomaly.ts, self._params.window_s),
            first_service=anomaly.service,
            first_detector=anomaly.detector,
            opened_at=anomaly.ts,
            last_anomaly_ts=anomaly.ts,
            severity=anomaly.severity,
        )
        self._active[record.id] = record
        self._add(record, anomaly)
        record.severity = self._classify(record)
        return record

    def _add(self, record: _Record, anomaly: Anomaly) -> None:
        record.service_set.add(anomaly.service)
        record.anomaly_count += 1
        record.anomalies = self._retain([*record.anomalies, anomaly])
        record.signals = [*record.signals, anomaly.signal_id][-self._max_signals :]
        record.last_anomaly_ts = max(record.last_anomaly_ts, anomaly.ts)
        record.dirty = True

    def _classify(self, record: _Record) -> Severity:
        """Coarse severity from what correlation sees (graph distance is a P5 refinement)."""
        p = self._params
        worst = max((a.severity for a in record.anomalies), key=_RANK.__getitem__, default=None)
        if (
            record.severity is Severity.CRITICAL
            or worst is Severity.CRITICAL
            or len(record.service_set) >= p.critical_services
            or record.anomaly_count >= p.critical_anomalies
        ):
            return Severity.CRITICAL
        return Severity.WARNING if record.severity is not Severity.INFO else record.severity

    def _renew(self, record: _Record, ts: datetime) -> None:
        """Keep every service of an active incident frozen (D4)."""
        for service in record.service_set:
            self._coordinator.freeze(service, ts)

    def _release(self, record: _Record) -> None:
        for service in record.service_set:
            still_active = any(service in other.service_set for other in self._active.values())
            if not still_active:
                self._coordinator.unfreeze(service)

    def _due(self, record: _Record, now: datetime) -> bool:
        """An evidence-only update is allowed once per `update_interval_s` of signal time."""
        if not record.dirty or record.last_published_at is None:
            return False
        return (now - record.last_published_at).total_seconds() >= self._params.update_interval_s

    def _retain(self, anomalies: list[Anomaly]) -> list[Anomaly]:
        """Keep the newest `max_per_detector` of each detector, at most `max_anomalies` overall."""
        seen: dict[str, int] = {}
        kept: list[Anomaly] = []
        for anomaly in reversed(anomalies):
            seen[anomaly.detector] = seen.get(anomaly.detector, 0) + 1
            if seen[anomaly.detector] <= self._max_per_detector:
                kept.append(anomaly)
        kept.reverse()
        return kept[-self._max_anomalies :]

    def _snapshot(self, record: _Record, *, at: datetime) -> Incident:
        record.revision += 1
        record.last_published_at = at
        record.dirty = False
        extra = len(record.service_set) - 1
        suffix = f" (+{extra} services)" if extra > 0 else ""
        closed = record.state in ("resolved", "expired")
        return Incident(
            id=record.id,
            revision=record.revision,
            state=record.state,
            opened_at=record.opened_at,
            updated_at=at,
            closed_at=at if closed else None,
            title=f"{record.first_service}: {record.first_detector} anomaly{suffix}",
            services=sorted(record.service_set),
            signals=list(record.signals),
            anomalies=list(record.anomalies),
            severity=record.severity,
            fingerprint=record.fingerprint,
        )


__all__ = ["IncidentTracker", "is_active"]
