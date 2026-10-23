"""Evidence bundles and ranked candidates — the RCA output shape (D15, surface-map).

Evidence is bundled **before** ranking, not after (D15): `build_evidence` runs during the walk so
the ranker scores against the same bundle the LLM will later summarise. Every claim the LLM
makes must cite an evidence id from the bundle (D1); the `id` field is that citation handle.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from inframind.common.anomaly import Anomaly
from inframind.common.ids import new_ulid
from inframind.common.signal import UNKNOWN_SERVICE, normalise_service

from .change_correlation import ChangeEvent

EvidenceKind = Literal["metric", "log_ref", "trace", "change", "baseline"]


class Evidence(BaseModel):
    """One proof-of-cause item attached to a candidate (surface-map "Incidents out")."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=new_ulid)
    kind: EvidenceKind
    summary: str
    ref: str
    weight: float = Field(ge=0.0)


class Candidate(BaseModel):
    """One possible root-cause service with its score and evidence bundle (D1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=new_ulid)
    service: str
    score: float
    depth: int = Field(ge=0)
    change_correlation: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("service")
    @classmethod
    def _service_canonical(cls, v: str) -> str:
        service = normalise_service(v)
        if service == UNKNOWN_SERVICE:
            raise ValueError("service must not be empty or unknown")
        return service


@dataclass
class CandidateBundle:
    """Working state for one candidate during the walk: anomalies + changes in scope."""

    service: str
    depth: int
    anomalies: list[Anomaly] = field(default_factory=list)
    changes: list[ChangeEvent] = field(default_factory=list)


def build_evidence(
    bundle: CandidateBundle,
    *,
    window: tuple[datetime, datetime],
    max_log_lines: int = 10,
) -> list[Evidence]:
    """Assemble the evidence bundle for one candidate (standard 06.6, D15).

    Built from the anomalies and change events collected during the walk. ``weight`` is how
    much each item moved the candidate's score; the ranker reads it as the anomaly severity
    and change-correlation contributions, so the bundle and the score stay consistent.

    Evidence is filtered to the incident window so a stale anomaly from an earlier incident
    cannot leak into this bundle. Returns items in a deterministic order: anomaly evidence
    first (by timestamp), then change evidence (by time).
    """
    opened_at, closed_at = window
    evidence: list[Evidence] = []

    for anomaly in sorted(bundle.anomalies, key=lambda a: (a.ts, a.id)):
        if not (opened_at <= anomaly.ts <= closed_at):
            continue
        evidence.append(
            Evidence(
                kind="metric",
                summary=f"{anomaly.detector} fired on {anomaly.service} (score={anomaly.score:.2f})",
                ref=anomaly.signal_id,
                weight=max(0.0, float(anomaly.score)),
            )
        )
        evidence.append(
            Evidence(
                kind="baseline",
                summary=f"pre-incident baseline for {anomaly.service}/{anomaly.detector}",
                ref=anomaly.id,
                weight=max(0.0, float(anomaly.score)) * 0.5,
            )
        )

    for change in sorted(bundle.changes, key=lambda c: (c.occurred_at, c.resource)):
        evidence.append(
            Evidence(
                kind="change",
                summary=f"{change.kind} on {change.resource} at {change.occurred_at.isoformat()}",
                ref=change.resource,
                weight=1.0,
            )
        )

    return evidence[: max_log_lines * 2] if max_log_lines else evidence


def anomalies_by_service(anomalies: Sequence[Anomaly]) -> dict[str, list[Anomaly]]:
    """Group anomalies by their service, each list ordered by timestamp then id."""
    grouped: dict[str, list[Anomaly]] = {}
    for anomaly in anomalies:
        grouped.setdefault(anomaly.service, []).append(anomaly)
    for service_anomalies in grouped.values():
        service_anomalies.sort(key=lambda a: (a.ts, a.id))
    return grouped


def changes_by_service(changes: Sequence[ChangeEvent]) -> dict[str, list[ChangeEvent]]:
    """Group change events by service, each list ordered by time."""
    grouped: dict[str, list[ChangeEvent]] = {}
    for change in changes:
        grouped.setdefault(change.service, []).append(change)
    for service_changes in grouped.values():
        service_changes.sort(key=lambda c: (c.occurred_at, c.resource))
    return grouped


def earliest_onset(anomalies: Sequence[Anomaly], opened_at: datetime) -> float:
    """Return the ``earliest_onset`` term in (0, 1] (standard 06.4).

    ``1 / (seconds since the first anomaly on this candidate, plus one)``. A candidate that
    fired at the moment the incident opened scores near 1.0; one that fired 10 minutes later
    scores near 0.1. A candidate with no anomalies scores 0.0.
    """
    if not anomalies:
        return 0.0
    first_ts = min(a.ts for a in anomalies)
    delta_s = max(0.0, (opened_at - first_ts).total_seconds())
    return 1.0 / (1.0 + delta_s / 60.0)


def downstream_depth(depth: int, max_depth: int) -> float:
    """Return the ``downstream_depth`` term in [0, 1] (standard 06.4).

    Deeper in the walk (further from the symptom, i.e. closer to the origin of the failure)
    scores higher, because a callee that is also anomalous is more likely the root cause than
    the symptom itself.
    """
    if max_depth <= 0:
        return 0.0
    return min(1.0, depth / max_depth)
