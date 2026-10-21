"""The alerting router: dedup, then fan out to the configured sinks.

This is the **in-process hand-off point** from RCA. P6 deliberately does NOT add a new
Redis stream key for RCA results — the RCA worker (P5) and this router run in the same
process and hand the ranked candidate list over as a Python object. That is the
simplest correct reading of D20 ("storage is the source of truth, the bus is the
conduit") and avoids a D-change to the pinned stream keys.

`AlertRouter.dispatch` is the single call the worker makes once the ranker has produced
candidates for an incident:

    await router.dispatch(incident, candidates, summary_text=summary.summary_text)

It:

1. Checks the dedup tracker (one alert per open incident, re-arms on escalation).
2. Builds the redacted payload.
3. Fires every enabled sink and collects the outcomes.
4. Returns a list of `(alert_id, channel, status)` so the caller can persist them into
   the `alerts` table (storage/incidents.py, called by the worker, not here — alerting
   may not import storage per the DAG).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import Literal

from inframind.common.incident import Incident
from inframind.common.logging import get_logger
from inframind.rca.evidence import Candidate

from . import sinks
from .config import AlertingParams
from .dedup import DedupTracker
from .payload import build_payload

logger = get_logger(__name__)

ChannelName = Literal["console", "webhook", "slack", "email"]
SinkStatus = Literal["delivered", "queued", "failed"]


@dataclass(frozen=True)
class DispatchResult:
    """What happened for one incident across all sinks."""

    incident_id: str
    alerted: bool  # False when dedup suppressed it
    outcomes: list[tuple[str, ChannelName, SinkStatus]]

    @property
    def delivered_any(self) -> bool:
        return any(status == "delivered" for _, _, status in self.outcomes)


class AlertRouter:
    """Dedup + fan-out. The one object the RCA worker holds."""

    def __init__(self, params: AlertingParams | None = None) -> None:
        self._params = params or AlertingParams()
        self._dedup = DedupTracker(window_s=self._params.dedup_window_s)

    async def dispatch(
        self,
        incident: Incident,
        candidates: Sequence[Candidate],
        *,
        summary_text: str | None = None,
    ) -> DispatchResult:
        """Fire one alert for `incident` if dedup allows, and return what happened."""
        if not self._dedup.should_alert(incident.id, incident.severity):
            logger.info(
                "alert_deduped",
                incident_id=incident.id,
                severity=incident.severity,
            )
            return DispatchResult(incident_id=incident.id, alerted=False, outcomes=[])

        payload = build_payload(incident, candidates, summary_text=summary_text)

        tasks: list[tuple[ChannelName, Awaitable[tuple[str, SinkStatus]]]] = []
        if self._params.console_enabled:
            tasks.append(("console", asyncio.to_thread(sinks.console.send, payload)))
        if self._params.webhook_url:
            tasks.append(
                (
                    "webhook",
                    sinks.webhook.send(
                        payload,
                        url=self._params.webhook_url,
                        timeout_s=self._params.webhook_timeout_s,
                    ),
                )
            )

        outcomes: list[tuple[str, ChannelName, SinkStatus]] = []
        for channel, coro in tasks:
            alert_id, status = await coro
            outcomes.append((alert_id, channel, status))

        self._dedup.record(incident.id, incident.severity)
        logger.info(
            "alert_dispatched",
            incident_id=incident.id,
            severity=incident.severity,
            sinks=[channel for _, channel, _ in outcomes],
            statuses=[status for _, _, status in outcomes],
        )
        return DispatchResult(incident_id=incident.id, alerted=True, outcomes=outcomes)


__all__ = ["AlertRouter", "DispatchResult"]
