"""Alertmanager webhook: the only push ingress. Maps webhook payloads to `Signal`s.

Alertmanager posts one notification per alert group, so a single request can carry many alerts;
each becomes its own `Signal`. Alerts that cannot be tied to a service resolve to `unknown` and
are dropped by the bus (and counted here), e.g. the always-firing `Watchdog`.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from redis.asyncio import Redis

from inframind.common.bus import publish
from inframind.common.exceptions import BusError
from inframind.common.logging import get_logger
from inframind.common.signal import (
    Severity,
    Signal,
    SignalType,
    Source,
    normalise_service,
    service_from_pod,
)
from inframind.common.time import utcnow

log = get_logger(__name__)

router = APIRouter()

_NO_END = "0001-01-01T00:00:00Z"
_SEVERITY = {"critical": Severity.CRITICAL, "warning": Severity.WARNING}


def _parse_ts(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as e:
        raise ValueError(f"alert {field} is not an ISO-8601 timestamp: {value!r}") from e
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _service(labels: dict[str, str]) -> str:
    """Resolve the affected service: deployment, then pod, then `service`, then scrape job.

    Workload labels win over `service` because the Prometheus Operator stamps `service` with the
    Service of the *scrape target* (e.g. `kube-prometheus-stack-kube-state-metrics` on every
    pod-level alert), which would blame the exporter instead of the workload.
    """
    if labels.get("deployment"):
        return normalise_service(labels["deployment"])
    if labels.get("pod"):
        return service_from_pod(labels["pod"])
    return normalise_service(labels.get("service") or labels.get("job", ""))


def _alert_to_signal(
    alert: Any, *, group_key: str, request_id: str, ingested_at: datetime
) -> Signal:
    if not isinstance(alert, dict) or not isinstance(alert.get("labels"), dict):
        raise ValueError("alert must be an object with a labels object")
    labels = {str(k): str(v) for k, v in alert["labels"].items()}
    status = str(alert.get("status", "firing"))
    resolved = status == "resolved"
    starts_at = _parse_ts(alert.get("startsAt"), "startsAt")
    ends_at = str(alert.get("endsAt", _NO_END))
    observed = _parse_ts(ends_at, "endsAt") if resolved and ends_at != _NO_END else starts_at
    return Signal(
        ts=observed,
        source=Source.ALERTMANAGER,
        type=SignalType.ALERT,
        service=_service(labels),
        severity=Severity.INFO
        if resolved
        else _SEVERITY.get(labels.get("severity", ""), Severity.INFO),
        attrs={
            "alertname": labels.get("alertname", ""),
            "status": status,
            "fingerprint": str(alert.get("fingerprint", "")),
            "labels": labels,
            "annotations": alert.get("annotations") or {},
            "starts_at": starts_at.isoformat(),
            "ends_at": None if ends_at == _NO_END else ends_at,
            "group_key": group_key,
            "request_id": request_id,
            "ingested_at": ingested_at.isoformat(),
        },
    )


def alerts_to_signals(
    payload: dict[str, Any], *, request_id: str, ingested_at: datetime
) -> list[Signal]:
    """Map an Alertmanager webhook payload to one `Signal` per alert.

    Raises:
        ValueError: the payload is not a webhook body, or an alert is malformed.
    """
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise ValueError("payload must contain an alerts list")
    group_key = str(payload.get("groupKey", ""))
    try:
        return [
            _alert_to_signal(a, group_key=group_key, request_id=request_id, ingested_at=ingested_at)
            for a in alerts
        ]
    except ValidationError as e:
        raise ValueError(f"alert failed Signal validation: {e}") from e


def _authorised(request: Request) -> bool:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    secret: str = request.app.state.webhook_secret
    return scheme.lower() == "bearer" and hmac.compare_digest(token.encode(), secret.encode())


@router.post("/webhooks/alertmanager")
async def alertmanager_webhook(request: Request) -> dict[str, int]:
    """Receive an Alertmanager notification and publish its alerts to `stream:signals`."""
    if not _authorised(request):
        raise HTTPException(status_code=401, detail="invalid webhook credentials")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        signals = alerts_to_signals(body, request_id=request_id, ingested_at=utcnow())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    redis: Redis = request.app.state.redis
    accepted = 0
    try:
        for signal in signals:
            if await publish(redis, signal) is not None:
                accepted += 1
    except BusError as e:
        log.error("alert_publish_failed", request_id=request_id, error=str(e))
        raise HTTPException(status_code=503, detail="event bus unavailable") from e
    return {"accepted": accepted, "dropped": len(signals) - accepted}
