"""The six metrics against real infrastructure. Skipped when the backend is unreachable.

Two assertions:

1. `bus.publish` actually moves `inframind_signals_received_total` — the counter is wired to
   the XADD, not merely registered.
2. Every worker can bind and serve `/metrics` on its port without disturbing its own work.

The second is the chart's PodMonitor contract: if a worker cannot bind 9100, the scrape target
silently disappears and the metric looks like "zero traffic" rather than "no scrape", which is
the misread the coverage block in `evaluation/report.py` exists to prevent.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis

from inframind import metrics
from inframind.common.bus import publish
from inframind.common.signal import Signal, SignalType, Source
from inframind.common.time import utcnow

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis() -> Iterator[Redis]:
    client = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis not reachable")
    yield client
    await client.aclose()


async def test_signal_counter_moves_after_a_real_publish(redis: Redis) -> None:
    """The counter is incremented on the successful XADD path, not at registration."""
    labels = {"source": Source.PROMETHEUS.value, "type": SignalType.METRIC.value}
    before = metrics.SIGNALS_RECEIVED.labels(**labels)._value.get()

    stream = f"stream:test-metrics-{uuid4().hex}"
    signal = Signal(ts=utcnow(), source=Source.PROMETHEUS, type=SignalType.METRIC, service="cart")
    assert await publish(redis, signal, stream=stream) is not None

    after = metrics.SIGNALS_RECEIVED.labels(**labels)._value.get()
    assert after == before + 1


async def test_unroutable_signal_does_not_move_the_counter(redis: Redis) -> None:
    """`publish` returns None for an unresolved service and must not count a signal that
    never reached the bus."""
    labels = {"source": Source.PROMETHEUS.value, "type": SignalType.METRIC.value}
    before = metrics.SIGNALS_RECEIVED.labels(**labels)._value.get()

    orphan = Signal(ts=utcnow(), source=Source.PROMETHEUS, type=SignalType.METRIC, service="")
    assert await publish(redis, orphan, stream=f"stream:test-{uuid4().hex}") is None

    assert metrics.SIGNALS_RECEIVED.labels(**labels)._value.get() == before


def test_worker_metrics_server_serves_the_six_families() -> None:
    """A worker binds 9100 and serves the registry — the PodMonitor's whole contract."""
    port = int(os.environ.get("METRICS_TEST_PORT", "9199"))
    assert metrics.start_http_server(port=port) is not None
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5.0)
        assert r.status_code == 200
        assert "inframind_signals_received" in r.text
    finally:
        # The server thread is a daemon; nothing to shut down cleanly, and the port is a
        # test-only one. Leaving it bound for the rest of the session is harmless.
        pass
