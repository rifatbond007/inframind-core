"""`publish_incident` writes to the pinned incidents stream and wraps Redis failures."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from redis import exceptions as redis_exc

from inframind.common import stream_keys
from inframind.common.bus import publish_incident
from inframind.common.exceptions import BusError
from inframind.common.incident import Incident
from inframind.common.signal import Severity

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def incident() -> Incident:
    return Incident(
        revision=1,
        state="open",
        opened_at=NOW,
        updated_at=NOW,
        closed_at=None,
        title="frontend: zscore anomaly",
        services=["frontend"],
        signals=["01SIG"],
        anomalies=[],
        severity=Severity.WARNING,
    )


async def test_publish_incident_writes_only_to_the_incidents_stream() -> None:
    redis = FakeAsyncRedis()
    sent = incident()
    entry_id = await publish_incident(redis, sent)
    assert entry_id
    [(_, fields)] = await redis.xrange(stream_keys.INCIDENTS)
    assert Incident.model_validate_json(fields[b"data"]) == sent
    assert await redis.xlen(stream_keys.SIGNALS) == 0
    assert await redis.xlen(stream_keys.GRAPH_UPDATES) == 0


async def test_publish_incident_failure_raises_bus_error() -> None:
    class Broken(FakeAsyncRedis):
        async def xadd(self, *args: Any, **kwargs: Any) -> Any:
            raise redis_exc.ConnectionError("down")

    with pytest.raises(BusError):
        await publish_incident(Broken(), incident())
