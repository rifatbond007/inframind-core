"""`/readyz` tests — the readiness probe reports backends honestly (D20).

The probe returns 200 only when Redis ping AND Postgres SELECT 1 both succeed; otherwise
503 with a JSON body naming the failing dependency. The test stubs `redis.ping` and the
pool's acquire without standing up either backend.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from inframind.api.app import create_app

SECRET = "s3cret"


@pytest.fixture
def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis()


@pytest.fixture
def healthy_pool() -> MagicMock:
    """An async-context-manager pool whose `SELECT 1` returns 1."""

    class _Conn:
        async def fetchval(self, *_args: object, **_kwargs: object) -> int:
            return 1

    class _Acquire:
        async def __aenter__(self) -> _Conn:
            return _Conn()

        async def __aexit__(self, *_: object) -> bool:
            return False

    class _Pool:
        def acquire(self) -> _Acquire:
            return _Acquire()

    return _Pool()


@pytest.fixture
def broken_pool() -> MagicMock:
    """An async-context-manager pool whose acquire fails with `OSError`."""

    class _Acquire:
        async def __aenter__(self) -> object:
            raise OSError("connection refused")

        async def __aexit__(self, *_: object) -> bool:
            return False

    class _Pool:
        def acquire(self) -> _Acquire:
            return _Acquire()

    return _Pool()


async def _client(redis: FakeAsyncRedis, *, pool: object | None) -> httpx.AsyncClient:
    app = create_app(redis, webhook_secret=SECRET, postgres_pool=pool)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://api")


async def test_readyz_200_when_both_redis_and_postgres_respond(
    redis: FakeAsyncRedis, healthy_pool: object
) -> None:
    """Happy path: Redis ping succeeds and the pool returns 1."""
    async with await _client(redis, pool=healthy_pool) as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["postgres"] == "ok"


async def test_readyz_503_when_postgres_pool_not_configured(redis: FakeAsyncRedis) -> None:
    """A pool of `None` is "not configured" — still 503 with a named check."""
    async with await _client(redis, pool=None) as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"] == "not_configured"
    assert body["checks"]["redis"] == "ok"


async def test_readyz_503_when_postgres_pool_raises(
    redis: FakeAsyncRedis, broken_pool: object
) -> None:
    """A pool that fails to acquire surfaces as `unavailable`, not a 500."""
    async with await _client(redis, pool=broken_pool) as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["postgres"] == "unavailable"


async def test_readyz_503_when_redis_ping_fails() -> None:
    """A Redis whose ping raises surfaces as 503 with `redis=unavailable`."""
    redis = FakeAsyncRedis()

    async def ping_raises() -> bool:
        raise OSError("redis is gone")

    with patch.object(redis, "ping", side_effect=ping_raises):
        app = create_app(redis, webhook_secret=SECRET, postgres_pool=None)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            resp = await client.get("/readyz")

    assert resp.status_code == 503
    assert resp.json()["checks"]["redis"] == "unavailable"


async def test_metrics_exposes_prometheus_text(redis: FakeAsyncRedis) -> None:
    """`/metrics` returns Prometheus text format with the process-up gauge."""
    async with await _client(redis, pool=None) as client:
        resp = await client.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "inframind_up 1" in body
    assert "inframind_uptime_seconds" in body
