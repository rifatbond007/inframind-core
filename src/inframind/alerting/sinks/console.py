"""The console sink: every alert becomes one structured log line.

This is the simplest sink and the one always on in dev. The log record is itself the
audit; the JSON line carries the alert id, the incident, the severity, and the full
redacted payload so a `kubectl logs` reader can see exactly what fired without needing
to query Postgres.
"""

from __future__ import annotations

import json
from typing import Literal

from inframind.common.ids import new_ulid
from inframind.common.logging import get_logger

ChannelName = Literal["console", "webhook", "slack", "email"]
SinkStatus = Literal["delivered", "queued", "failed"]

logger = get_logger(__name__)


def send(payload: dict[str, object]) -> tuple[str, SinkStatus]:
    """Emit `payload` to the structured logger. Returns `(alert_id, 'delivered')`."""
    alert_id = new_ulid()
    logger.info(
        "alert_dispatched",
        alert_id=alert_id,
        incident_id=payload.get("incident_id"),
        severity=payload.get("severity"),
        payload=json.loads(json.dumps(payload)),
    )
    return alert_id, "delivered"


__all__ = ["ChannelName", "SinkStatus", "send"]
