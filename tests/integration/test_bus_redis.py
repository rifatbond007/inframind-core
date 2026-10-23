"""Bus against a real Redis (`make up` / docker compose). Skipped when Redis is unreachable."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from inframind.common.bus import SignalConsumer, publish
from inframind.common.signal import Signal, SignalType, Source
from inframind.common.time import utcnow

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis() -> Redis:
    client = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis not reachable")
    yield client
    await client.aclose()


async def test_round_trip_and_dedup_on_real_redis(redis: Redis) -> None:
    stream = f"stream:test-{uuid4().hex}"
    s = Signal(ts=utcnow(), source=Source.PROMETHEUS, type=SignalType.METRIC, service="cart")
    try:
        await publish(redis, s, stream=stream)
        await publish(redis, s, stream=stream)
        c = SignalConsumer(redis, "g", "c1", stream=stream, block_ms=50)
        await c.ensure_group()
        got: list[Signal] = []

        async def handler(x: Signal) -> None:
            got.append(x)

        await c.poll_once(handler)
        assert got == [s]
    finally:
        keys = [stream, *[k async for k in redis.scan_iter(f"dedup:{stream}:*")]]
        await redis.delete(*keys)
