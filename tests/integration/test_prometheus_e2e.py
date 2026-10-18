"""Prometheus collector against the live testbed. Skipped when Prometheus or Redis is down."""

from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest
from inframind.common.signal import Signal, Source
from inframind.ingestion.prometheus import PrometheusCollector
from redis.asyncio import Redis

pytestmark = pytest.mark.integration

PROM_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:30090")


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


@pytest.fixture
async def prom() -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=PROM_URL, timeout=httpx.Timeout(10.0))
    try:
        (await client.get("/api/v1/status/runtimeinfo")).raise_for_status()
    except httpx.HTTPError:
        await client.aclose()
        pytest.skip("Prometheus not reachable")
    yield client
    await client.aclose()


async def test_live_poll_publishes_valid_signals(prom: httpx.AsyncClient, redis: Redis) -> None:
    stream = f"stream:test-{uuid4().hex}"
    try:
        published = await PrometheusCollector(prom, redis, stream=stream).poll_once()
        entries = await redis.xrange(stream)
        signals = [Signal.model_validate_json(f[b"data"]) for _, f in entries]
        assert published == len(signals) > 0
        assert all(s.source is Source.PROMETHEUS and s.service != "unknown" for s in signals)
        assert {"cpu_cores", "memory_working_set_bytes"} & {s.attrs["metric"] for s in signals}
    finally:
        await redis.delete(stream)
