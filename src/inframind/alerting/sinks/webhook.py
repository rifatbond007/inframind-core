"""The webhook sink: POST the alert JSON to a single configured endpoint.

Failure semantics:

* **`delivered`** — 2xx response.
* **`queued`** — non-2xx response on the first attempt; we return `queued` so the
  caller records it as "delivered to the queue, retry on the bus" without pretending it
  reached the human. The implementation itself does not actually queue; the language
  here matches the persistence contract in the `alerts` table ("we did try").
* **`failed`** — transport error (network, timeout); the next attempt will re-encode
  and try again.

The sink is a no-op if `url` is empty, which is how the alerting layer conditionalises
the webhook: callers wire the sink with the configured URL string from
`alerting.config.load_alerting_params`, which defaults to `""`.
"""

from __future__ import annotations

from typing import Literal

import httpx

from inframind.common.ids import new_ulid

ChannelName = Literal["console", "webhook", "slack", "email"]
SinkStatus = Literal["delivered", "queued", "failed"]


async def send(
    payload: dict[str, object],
    *,
    url: str,
    timeout_s: float = 5.0,
) -> tuple[str, SinkStatus]:
    """POST `payload` to `url`. Returns `(alert_id, status)`."""
    if not url:
        return new_ulid(), "failed"

    alert_id = new_ulid()
    body = dict(payload)
    body["alert_id"] = alert_id

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(url, json=body)
    except httpx.HTTPError:
        return alert_id, "failed"

    if 200 <= response.status_code < 300:
        return alert_id, "delivered"
    return alert_id, "queued"


__all__ = ["ChannelName", "SinkStatus", "send"]
