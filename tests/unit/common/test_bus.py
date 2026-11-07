"""Bus behaviour against fakeredis: publish, group reads, idempotence, redelivery, poison."""

from __future__ import annotations

import asyncio

import pytest
from fakeredis import FakeAsyncRedis

from inframind.common import stream_keys
from inframind.common.bus import SignalConsumer, publish
from inframind.common.exceptions import BusError
from inframind.common.signal import Signal, SignalType, Source
from inframind.common.time import utcnow


@pytest.fixture
async def redis() -> FakeAsyncRedis:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


def sig(service: str = "frontend") -> Signal:
    return Signal(
        ts=utcnow(), source=Source.LOKI, type=SignalType.LOG, service=service, attrs={"n": 1}
    )


def consumer(redis: FakeAsyncRedis, name: str = "c1", **kw: int) -> SignalConsumer:
    return SignalConsumer(redis, "detect", name, block_ms=10, **kw)


async def test_publish_then_consume_round_trips(redis: FakeAsyncRedis) -> None:
    s = sig()
    assert await publish(redis, s) is not None
    c = consumer(redis)
    await c.ensure_group()
    got: list[Signal] = []

    async def handler(x: Signal) -> None:
        got.append(x)

    assert await c.poll_once(handler) == 1
    assert got == [s]
    assert (await redis.xpending(stream_keys.SIGNALS, "detect"))["pending"] == 0


async def test_unknown_service_dropped_not_published(redis: FakeAsyncRedis) -> None:
    assert await publish(redis, sig(service="")) is None
    assert await redis.xlen(stream_keys.SIGNALS) == 0


async def test_publish_caps_stream_length(redis: FakeAsyncRedis) -> None:
    for _ in range(20):
        await publish(redis, sig(), maxlen=5)
    assert 5 <= await redis.xlen(stream_keys.SIGNALS) <= 20


async def test_duplicate_signal_id_handled_once(redis: FakeAsyncRedis) -> None:
    s = sig()
    await publish(redis, s)
    await publish(redis, s)  # same Signal.id published twice (e.g. collector retry)
    c = consumer(redis)
    await c.ensure_group()
    calls = 0

    async def handler(_: Signal) -> None:
        nonlocal calls
        calls += 1

    await c.poll_once(handler)
    assert calls == 1
    assert (await redis.xpending(stream_keys.SIGNALS, "detect"))["pending"] == 0


async def test_failed_handler_leaves_entry_pending_and_redelivers(
    redis: FakeAsyncRedis,
) -> None:
    await publish(redis, sig())
    c = consumer(redis, reclaim_idle_ms=0)
    await c.ensure_group()
    attempts = 0

    async def flaky(_: Signal) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")

    assert await c.poll_once(flaky) == 0
    assert (await redis.xpending(stream_keys.SIGNALS, "detect"))["pending"] == 1
    assert await c.poll_once(flaky) == 1
    assert attempts == 2
    assert (await redis.xpending(stream_keys.SIGNALS, "detect"))["pending"] == 0


async def test_poison_message_acked_and_skipped(redis: FakeAsyncRedis) -> None:
    await redis.xadd(stream_keys.SIGNALS, {"data": "{not json"})
    await publish(redis, sig())
    c = consumer(redis)
    await c.ensure_group()
    got: list[Signal] = []

    async def handler(x: Signal) -> None:
        got.append(x)

    assert await c.poll_once(handler) == 1
    assert len(got) == 1
    assert (await redis.xpending(stream_keys.SIGNALS, "detect"))["pending"] == 0


async def test_groups_each_get_every_signal(redis: FakeAsyncRedis) -> None:
    await publish(redis, sig())
    seen: dict[str, int] = {"detect": 0, "audit": 0}
    for group in seen:
        c = SignalConsumer(redis, group, "c1", block_ms=10)
        await c.ensure_group()

        async def handler(_: Signal, g: str = group) -> None:
            seen[g] += 1

        await c.poll_once(handler)
    assert seen == {"detect": 1, "audit": 1}


async def test_ensure_group_is_idempotent(redis: FakeAsyncRedis) -> None:
    c = consumer(redis)
    await c.ensure_group()
    await c.ensure_group()


async def test_run_stops_on_event(redis: FakeAsyncRedis) -> None:
    await publish(redis, sig())
    c = consumer(redis)
    stop = asyncio.Event()
    got: list[Signal] = []

    async def handler(x: Signal) -> None:
        got.append(x)
        stop.set()

    await asyncio.wait_for(c.run(handler, stop), timeout=5)
    assert len(got) == 1


async def test_publish_wraps_redis_errors() -> None:
    from redis.asyncio import Redis

    dead = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    with pytest.raises(BusError):
        await publish(dead, sig())
    await dead.aclose()


async def test_a_group_started_at_the_tail_ignores_existing_entries(redis: FakeAsyncRedis) -> None:
    for _ in range(3):
        await publish(redis, sig())
    tail = SignalConsumer(redis, "tail", "c1", block_ms=10, start_id="$")
    await tail.ensure_group()
    seen: list[Signal] = []

    async def handler(s: Signal) -> None:
        seen.append(s)

    assert await tail.poll_once(handler) == 0
    fresh = sig()
    await publish(redis, fresh)
    assert await tail.poll_once(handler) == 1
    assert seen == [fresh]


async def test_a_default_group_still_reads_from_the_beginning(redis: FakeAsyncRedis) -> None:
    await publish(redis, sig())
    head = SignalConsumer(redis, "head", "c1", block_ms=10)
    await head.ensure_group()
    assert await head.poll_once(lambda s: asyncio.sleep(0)) == 1
