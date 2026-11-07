"""The RCA ``w4`` term: correlate a candidate with Kubernetes changes (D22, standard 14).

`ChangeEvent` is deliberately metadata-only: **no `Secret` value, no request body, no env value
is ever stored** (D9). `change_correlation` implements the pinned scoring algorithm from
standard 14.7 exactly — filter to the window, weight by recency to `incident.opened_at`, sum,
then soft-saturate so a noisy service with many rollouts does not dominate.

The `w4` term is one of four in the score formula; see `rank.py` and standard 06.4.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from inframind.common.signal import UNKNOWN_SERVICE, normalise_service

ChangeKind = Literal[
    "deployment_rollout",
    "configmap_change",
    "secret_change",
    "hpa_change",
    "statefulset_rollout",
    "daemonset_rollout",
]


class ChangeEvent(BaseModel):
    """One observed Kubernetes change. Metadata only — never Secret contents (D9).

    ``kind`` is the resource class (``deployment_rollout``, ``configmap_change``, ...).
    ``resource`` names the object (e.g. ``deploy/frontend``). Nothing about the object's
    contents is retained.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    kind: ChangeKind
    resource: str
    occurred_at: datetime
    revision: str | None = Field(default=None)

    @field_validator("service")
    @classmethod
    def _service_canonical(cls, v: str) -> str:
        service = normalise_service(v)
        if service == UNKNOWN_SERVICE:
            raise ValueError("service must not be empty or unknown")
        return service

    @field_validator("occurred_at")
    @classmethod
    def _aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return v.astimezone(UTC)


def change_correlation(
    candidate_service: str,
    incident_window: tuple[datetime, datetime],
    change_events: Sequence[ChangeEvent],
    pre_window_s: float = 1800.0,
) -> float:
    """Return a change-correlation score in [0, 1] for `candidate_service`.

    ``incident_window`` is ``(opened_at, closed_at)``. Events are filtered to those on the
    candidate whose ``occurred_at`` falls in ``[opened_at - pre_window_s, closed_at]``.

    The pinned algorithm (standard 14.7):

    1. Filter to the window (pre-window catches a rollout just before the incident; the
       post-window extends to ``closed_at`` to catch a late HPA scale-up).
    2. Recency weight per event: ``max(0, 1 - delta_s / pre_window_s)`` where ``delta_s`` is
       the absolute distance from the event to ``opened_at``. Events closer to the incident
       open time score higher.
    3. Sum the recency weights (``raw``).
    4. Soft-saturate: ``raw / (1 + 0.5 * (n_events - 1))`` — two events count as 1.5x one,
       three as 2.0x, and so on, so a noisy service does not dominate.
    5. Cap at 1.0.

    Returns 0.0 when there are no relevant events in the window.
    """
    service = normalise_service(candidate_service)
    opened_at, closed_at = incident_window

    window_start = opened_at - timedelta(seconds=pre_window_s)
    in_window = [
        e
        for e in change_events
        if e.service == service and window_start <= e.occurred_at <= closed_at
    ]
    if not in_window:
        return 0.0

    raw = 0.0
    for event in in_window:
        delta_s = abs((event.occurred_at - opened_at).total_seconds())
        recency = max(0.0, 1.0 - (delta_s / pre_window_s))
        raw += recency

    n_events = len(in_window)
    normalised = raw / (1.0 + 0.5 * (n_events - 1)) if n_events >= 1 else 0.0
    return min(1.0, normalised)
