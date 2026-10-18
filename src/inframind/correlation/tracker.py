"""Minimal per-service incident tracker (P4). P4b adds windows, fingerprints and grouping.

One open incident per service. The first anomaly opens it and freezes the service's baselines
(D4); every further anomaly renews the freeze lease and updates the incident; after `cooldown_s`
without an anomaly (measured on signal time, so replays match live runs) it resolves and the
baselines are released. Snapshots are published on open, on a severity increase, and on resolve;
other updates are folded into the in-memory record and appear in the next snapshot. `revision`
counts publishes. The interface (`on_anomaly`, `tick`) is what P4b keeps. Retained anomalies are capped per detector, then overall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from inframind.common.anomaly import Anomaly
from inframind.common.ids import new_ulid
from inframind.common.incident import Incident, IncidentState
from inframind.common.signal import Severity
from inframind.detection.freeze import BaselineFreezeCoordinator

from .fingerprint import fingerprint

_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


@dataclass
class _Record:
    id: str | None
    service: str
    title: str
    opened_at: datetime
    last_anomaly_ts: datetime
    severity: Severity
    fingerprint: str = ""
    revision: int = 0
    anomalies: list[Anomaly] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)


class IncidentTracker:
    """Turns anomalies into per-service `Incident` snapshots and drives the baseline freeze."""

    def __init__(
        self,
        coordinator: BaselineFreezeCoordinator,
        *,
        cooldown_s: float = 120.0,
        max_anomalies: int = 50,
        max_per_detector: int = 10,
        max_signals: int = 200,
    ) -> None:
        self._coordinator = coordinator
        self._cooldown_s = cooldown_s
        self._max_anomalies = max_anomalies
        self._max_per_detector = max_per_detector
        self._max_signals = max_signals
        self._open: dict[str, _Record] = {}

    @property
    def open_services(self) -> frozenset[str]:
        """The services that currently have an open incident."""
        return frozenset(self._open)

    def on_anomaly(self, anomaly: Anomaly) -> list[Incident]:
        """Fold `anomaly` into its service's incident; return the snapshots to publish."""
        record = self._open.get(anomaly.service)
        opened = record is None
        if record is None:
            record = _Record(
                id=None,
                service=anomaly.service,
                title=f"{anomaly.service}: {anomaly.detector} anomaly",
                opened_at=anomaly.ts,
                last_anomaly_ts=anomaly.ts,
                severity=anomaly.severity,
                fingerprint=fingerprint(anomaly.service, anomaly.ts, 300.0),
            )
            self._open[anomaly.service] = record
        record.anomalies = self._retain([*record.anomalies, anomaly])
        record.signals = [*record.signals, anomaly.signal_id][-self._max_signals :]
        record.last_anomaly_ts = max(record.last_anomaly_ts, anomaly.ts)
        self._coordinator.freeze(anomaly.service, anomaly.ts)
        raised = _RANK[anomaly.severity] > _RANK[record.severity]
        if raised:
            record.severity = anomaly.severity
        if opened or raised:
            return [self._snapshot(record, state="open", at=anomaly.ts)]
        return []

    def tick(self, now: datetime) -> list[Incident]:
        """Resolve incidents quiet for `cooldown_s`; return their final snapshots."""
        resolved: list[Incident] = []
        for service in sorted(self._open):
            record = self._open[service]
            if (now - record.last_anomaly_ts).total_seconds() < self._cooldown_s:
                continue
            resolved.append(self._snapshot(record, state="resolved", at=now))
            del self._open[service]
            self._coordinator.unfreeze(service)
        return resolved

    def _retain(self, anomalies: list[Anomaly]) -> list[Anomaly]:
        """Keep the newest `max_per_detector` of each detector, at most `max_anomalies` overall.

        One noisy detector (a burst can fire an error-spike anomaly per error event) must not
        evict the evidence the others gave, so the cap is per detector first.
        """
        seen: dict[str, int] = {}
        kept: list[Anomaly] = []
        for anomaly in reversed(anomalies):
            seen[anomaly.detector] = seen.get(anomaly.detector, 0) + 1
            if seen[anomaly.detector] <= self._max_per_detector:
                kept.append(anomaly)
        kept.reverse()
        return kept[-self._max_anomalies :]

    def _snapshot(self, record: _Record, *, state: IncidentState, at: datetime) -> Incident:
        record.revision += 1
        incident = Incident(
            id=record.id or new_ulid(),
            revision=record.revision,
            state=state,
            opened_at=record.opened_at,
            updated_at=at,
            closed_at=at if state == "resolved" else None,
            title=record.title,
            services=[record.service],
            signals=list(record.signals),
            anomalies=list(record.anomalies),
            severity=record.severity,
            fingerprint=record.fingerprint,
        )
        record.id = incident.id
        return incident
