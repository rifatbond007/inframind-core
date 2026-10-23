"""Collector state: cursors and seen-keys that survive a restart (Redis) or not (memory)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from redis import exceptions as redis_exc

from inframind.common.collector_state import CollectorState, MemoryState, State
from inframind.common.exceptions import BusError


def redis_state() -> State:
    return CollectorState(FakeAsyncRedis(), "loki")


def memory_state() -> State:
    return MemoryState()


@pytest.fixture(params=[redis_state, memory_state], ids=["redis", "memory"])
def state(request: pytest.FixtureRequest) -> State:
    factory: Callable[[], State] = request.param
    return factory()


async def test_cursor_is_none_until_saved_then_round_trips(state: State) -> None:
    assert await state.load_cursor() is None
    await state.save_cursor(1_792_000_000_123_456_789)
    assert await state.load_cursor() == 1_792_000_000_123_456_789


async def test_mark_seen_is_true_only_the_first_time(state: State) -> None:
    assert not await state.is_seen("trace-1")
    assert await state.mark_seen("trace-1") is True
    assert await state.is_seen("trace-1")
    assert await state.mark_seen("trace-1") is False


async def test_state_is_namespaced_by_collector_name() -> None:
    redis = FakeAsyncRedis()
    loki, jaeger = CollectorState(redis, "loki"), CollectorState(redis, "jaeger")
    await loki.save_cursor(5)
    await loki.mark_seen("k")
    assert await jaeger.load_cursor() is None
    assert not await jaeger.is_seen("k")


async def test_a_second_instance_with_the_same_name_sees_the_same_state() -> None:
    redis = FakeAsyncRedis()
    await CollectorState(redis, "jaeger").mark_seen("t1")
    await CollectorState(redis, "jaeger").save_cursor(9)
    again = CollectorState(redis, "jaeger")
    assert await again.is_seen("t1")
    assert await again.load_cursor() == 9


async def test_seen_keys_expire() -> None:
    redis = FakeAsyncRedis()
    await CollectorState(redis, "loki", ttl_s=120).mark_seen("k")
    ttl = await redis.ttl("seen:loki:k")
    assert 0 < ttl <= 120


async def test_memory_state_forgets_the_oldest_keys_beyond_its_bound() -> None:
    mem = MemoryState(max_entries=3)
    for key in ("a", "b", "c", "d", "e"):
        await mem.mark_seen(key)
    assert await mem.mark_seen("e") is False
    assert await mem.mark_seen("a") is True


class Broken(FakeAsyncRedis):
    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise redis_exc.ConnectionError("down")

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise redis_exc.ConnectionError("down")

    async def exists(self, *args: Any, **kwargs: Any) -> Any:
        raise redis_exc.ConnectionError("down")


@pytest.mark.parametrize("call", ["load_cursor", "save_cursor", "mark_seen", "is_seen"])
async def test_redis_failures_surface_as_bus_error(call: str) -> None:
    state = CollectorState(Broken(), "loki")
    args = {"load_cursor": (), "save_cursor": (1,), "mark_seen": ("k",), "is_seen": ("k",)}[call]
    with pytest.raises(BusError):
        await getattr(state, call)(*args)
