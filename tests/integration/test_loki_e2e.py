"""Loki collector against the live testbed. Skipped when Loki or Redis is down."""

from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis

from inframind.common.signal import Signal, SignalType, Source
from inframind.ingestion.loki import LokiCollector

pytestmark = pytest.mark.integration

LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:30100")


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
async def loki() -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=LOKI_URL, timeout=httpx.Timeout(15.0))
    try:
        (await client.get("/loki/api/v1/labels")).raise_for_status()
    except httpx.HTTPError:
        await client.aclose()
        pytest.skip("Loki not reachable")
    yield client
    await client.aclose()


async def test_live_poll_publishes_only_valid_problem_signals(
    loki: httpx.AsyncClient, redis: Redis
) -> None:
    """Whatever warning/error lines the last hour holds must map to well-formed Signals."""
    stream = f"stream:test-{uuid4().hex}"
    try:
        collector = LokiCollector(loki, redis, initial_lookback_s=3600, stream=stream)
        published = await collector.poll_once()
        entries = await redis.xrange(stream)
        signals = [Signal.model_validate_json(f[b"data"]) for _, f in entries]
        assert published == len(signals)
        for s in signals:
            assert s.source is Source.LOKI
            assert s.type is SignalType.LOG
            assert s.service != "unknown"
            assert s.attrs["level"] in {"warning", "error"}
        # A new error line may legitimately arrive between polls; republishing one must not.
        await collector.poll_once()
        entries = await redis.xrange(stream)
        keys = [
            (s.ts, s.attrs["pod"], s.attrs["message"])
            for s in (Signal.model_validate_json(f[b"data"]) for _, f in entries)
        ]
        assert len(keys) == len(set(keys))
    finally:
        await redis.delete(stream)
