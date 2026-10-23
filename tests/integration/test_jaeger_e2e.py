"""Jaeger collector against the live testbed. Skipped when Jaeger or Redis is down."""

from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis

from inframind.common.graph_update import GraphUpdate
from inframind.common.signal import Signal, SignalType, Source
from inframind.ingestion.jaeger import JaegerCollector

pytestmark = pytest.mark.integration

JAEGER_URL = os.environ.get("JAEGER_URL", "http://localhost:30686")


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
async def jaeger() -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=JAEGER_URL, timeout=httpx.Timeout(20.0))
    try:
        (await client.get("/api/services")).raise_for_status()
    except httpx.HTTPError:
        await client.aclose()
        pytest.skip("Jaeger not reachable")
    yield client
    await client.aclose()


async def test_live_poll_publishes_well_formed_updates_and_signals(
    jaeger: httpx.AsyncClient, redis: Redis
) -> None:
    """Whatever traces the last hour holds must map to valid updates and Signals, once."""
    tag = uuid4().hex
    signals_stream, graph_stream = f"stream:test-sig-{tag}", f"stream:test-graph-{tag}"
    try:
        collector = JaegerCollector(
            jaeger,
            redis,
            initial_lookback_s=3600,
            settle_s=1,
            signals_stream=signals_stream,
            graph_stream=graph_stream,
        )
        published = await collector.poll_once()
        updates = [
            GraphUpdate.model_validate_json(f[b"data"]) for _, f in await redis.xrange(graph_stream)
        ]
        signals = [
            Signal.model_validate_json(f[b"data"]) for _, f in await redis.xrange(signals_stream)
        ]
        assert published == len(updates) + len(signals)
        assert all(u.caller != u.callee for u in updates)
        assert all(s.source is Source.JAEGER and s.type is SignalType.TRACE for s in signals)
        assert all(s.service != "unknown" for s in signals)
        # Live traffic (kubelet probes) keeps producing new traces, so a second poll may publish
        # *new* items; what it must never do is publish the same item twice.
        await collector.poll_once()
        updates = [
            GraphUpdate.model_validate_json(f[b"data"]) for _, f in await redis.xrange(graph_stream)
        ]
        signals = [
            Signal.model_validate_json(f[b"data"]) for _, f in await redis.xrange(signals_stream)
        ]
        # microsecond starts can collide across concurrent requests, a start plus exact latency cannot
        update_keys = [(u.caller, u.callee, u.observed_at, u.latency_ms) for u in updates]
        error_spans = [s.attrs["span_id"] for s in signals if "span_id" in s.attrs]
        assert len(update_keys) == len(set(update_keys))
        assert len(error_spans) == len(set(error_spans))
    finally:
        await redis.delete(signals_stream, graph_stream)
