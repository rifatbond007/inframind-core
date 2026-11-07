"""The shared collector runner: env-driven construction, clean shutdown, resource cleanup."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from inframind.ingestion import runner


class RecordingRedis(FakeAsyncRedis):
    closed = False

    async def aclose(self, *args: Any, **kwargs: Any) -> None:
        self.closed = True
        await super().aclose(*args, **kwargs)


class RedisFactory:
    """Stands in for the `Redis` class so no real connection is attempted."""

    instance: RecordingRedis
    url: str

    @classmethod
    def from_url(cls, url: str) -> RecordingRedis:
        cls.url = url
        cls.instance = RecordingRedis()
        return cls.instance


class FakeCollector:
    def __init__(self, client: httpx.AsyncClient, redis: Any, interval_s: int) -> None:
        self.client, self.redis, self.interval_s = client, redis, interval_s
        self.ran = False

    async def run(self, stop: asyncio.Event) -> None:
        self.ran = True


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> type[RedisFactory]:
    monkeypatch.setattr(runner, "Redis", RedisFactory)
    return RedisFactory


async def test_runner_builds_with_env_values_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch, factory: type[RedisFactory]
) -> None:
    monkeypatch.setenv("LOKI_URL", "http://loki.test:3100")
    monkeypatch.setenv("LOKI_INTERVAL_S", "7")
    monkeypatch.setenv("REDIS_URL", "redis://redis.test:6380/2")
    built: list[FakeCollector] = []

    def build(client: httpx.AsyncClient, redis: Any, interval_s: int) -> FakeCollector:
        built.append(FakeCollector(client, redis, interval_s))
        return built[-1]

    await runner.run_collector(
        build,
        url_env="LOKI_URL",
        default_url="http://x",
        interval_env="LOKI_INTERVAL_S",
        stop=asyncio.Event(),
    )
    [collector] = built
    assert collector.ran
    assert str(collector.client.base_url).rstrip("/") == "http://loki.test:3100"
    assert collector.interval_s == 7
    assert factory.url == "redis://redis.test:6380/2"
    assert collector.client.is_closed
    assert factory.instance.closed


async def test_runner_defaults_apply_when_env_is_unset(
    monkeypatch: pytest.MonkeyPatch, factory: type[RedisFactory]
) -> None:
    for name in ("JAEGER_URL", "JAEGER_INTERVAL_S", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    built: list[FakeCollector] = []

    def build(client: httpx.AsyncClient, redis: Any, interval_s: int) -> FakeCollector:
        built.append(FakeCollector(client, redis, interval_s))
        return built[-1]

    await runner.run_collector(
        build,
        url_env="JAEGER_URL",
        default_url="http://localhost:30686",
        interval_env="JAEGER_INTERVAL_S",
        stop=asyncio.Event(),
    )
    assert str(built[0].client.base_url).rstrip("/") == "http://localhost:30686"
    assert built[0].interval_s == 30
    assert factory.url == "redis://localhost:6379/0"


async def test_resources_are_closed_even_when_the_collector_crashes(
    monkeypatch: pytest.MonkeyPatch, factory: type[RedisFactory]
) -> None:
    class Crashing(FakeCollector):
        async def run(self, stop: asyncio.Event) -> None:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await runner.run_collector(
            lambda c, r, i: Crashing(c, r, i),
            url_env="X_URL",
            default_url="http://x",
            interval_env="X_INT",
            stop=asyncio.Event(),
        )
    assert factory.instance.closed
