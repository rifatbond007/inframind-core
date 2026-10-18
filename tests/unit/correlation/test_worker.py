"""CorrelationWorker: signals in, incidents out, resolution by signal time and by idle ticks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from redis import exceptions as redis_exc

from inframind.common import stream_keys
from inframind.common.bus import publish
from inframind.common.exceptions import BusError
from inframind.common.incident import Incident
from inframind.common.signal import Signal, SignalType, Source
from inframind.correlation.tracker import IncidentTracker
from inframind.correlation.worker import CorrelationWorker
from inframind.detection.engine import DetectionEngine


def metric(value: float, seconds: float, service: str = "frontend") -> Signal:
    return Signal(
        ts=datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds),
        source=Source.PROMETHEUS,
        type=SignalType.METRIC,
        service=service,
        attrs={"metric": "cpu_cores", "value": value},
    )


def warm(service: str = "frontend", n: int = 20) -> list[Signal]:
    return [metric(0.010 if k % 2 == 0 else 0.014, 30 * k, service) for k in range(n)]


def make(
    redis: FakeAsyncRedis, *, clock: Any = None, live_window_s: float = 600.0
) -> CorrelationWorker:
    engine = DetectionEngine.default({})
    tracker = IncidentTracker(engine.coordinator, cooldown_s=120.0)
    kwargs: dict[str, Any] = {"live_window_s": live_window_s, "block_ms": 10}
    if clock is not None:
        kwargs["clock"] = clock
    return CorrelationWorker(redis, engine, tracker, **kwargs)


async def drain(worker: CorrelationWorker) -> None:
    while await worker.poll_once():
        pass


async def incidents(redis: FakeAsyncRedis) -> list[Incident]:
    entries = await redis.xrange(stream_keys.INCIDENTS)
    return [Incident.model_validate_json(f[b"data"]) for _, f in entries]


async def run(redis: FakeAsyncRedis, worker: CorrelationWorker, signals: list[Signal]) -> None:
    await worker.ensure_group()
    for s in signals:
        await publish(redis, s)
    await drain(worker)


async def test_signals_in_incident_out() -> None:
    redis = FakeAsyncRedis()
    worker = make(redis)
    await run(redis, worker, [*warm(), metric(0.2, 600)])
    [incident] = await incidents(redis)
    assert incident.state == "open" and incident.revision == 1
    assert incident.services == ["frontend"]
    assert {a.detector for a in incident.anomalies} >= {"zscore"}


async def test_quiet_signals_publish_nothing() -> None:
    redis = FakeAsyncRedis()
    worker = make(redis)
    await run(redis, worker, warm(n=40))
    assert await incidents(redis) == []


async def test_resolution_by_signal_time() -> None:
    redis = FakeAsyncRedis()
    worker = make(redis)
    await run(redis, worker, [*warm(), metric(0.2, 600), metric(0.01, 600 + 130, "cartservice")])
    states = [(i.state, i.revision) for i in await incidents(redis)]
    assert states == [("open", 1), ("resolved", 2)]


async def test_idle_tick_resolves_when_the_stream_is_quiet() -> None:
    redis = FakeAsyncRedis()
    spike = metric(0.2, 600)
    now = {"t": spike.ts}
    worker = make(redis, clock=lambda: now["t"])
    await run(redis, worker, [*warm(), spike])
    assert [i.state for i in await incidents(redis)] == ["open"]
    now["t"] = spike.ts + timedelta(seconds=119)
    await worker.tick_idle()
    assert "resolved" not in [i.state for i in await incidents(redis)]  # evidence updates are fine
    now["t"] = spike.ts + timedelta(seconds=121)
    await worker.tick_idle()
    assert [i.state for i in await incidents(redis)][-1] == "resolved"


async def test_idle_tick_does_not_resolve_a_replay() -> None:
    redis = FakeAsyncRedis()
    spike = metric(0.2, 600)
    worker = make(redis, clock=lambda: spike.ts + timedelta(hours=1))  # signals are an hour old
    await run(redis, worker, [*warm(), spike])
    await worker.tick_idle()
    assert [i.state for i in await incidents(redis)] == ["open"]


async def test_a_publish_failure_does_not_lose_the_incident() -> None:
    class FlakyIncidents(FakeAsyncRedis):
        failures = 0

        async def xadd(self, name: Any, *args: Any, **kwargs: Any) -> Any:
            if name == stream_keys.INCIDENTS and self.failures > 0:
                self.failures -= 1
                raise redis_exc.ConnectionError("down")
            return await super().xadd(name, *args, **kwargs)

    redis = FlakyIncidents()
    worker = make(redis)
    for s in warm():
        await worker.handle(s)
    redis.failures = 1
    spike = metric(0.2, 600)
    with pytest.raises(BusError):
        await worker.handle(spike)  # the consumer would keep this entry pending
    assert await incidents(redis) == []
    await worker.handle(spike)  # redelivery: the detectors ignore it, the outbox flushes
    [incident] = await incidents(redis)
    assert incident.state == "open"
    await worker.handle(metric(0.01, 630, "cartservice"))
    published = await incidents(redis)
    assert {i.id for i in published} == {incident.id}  # still one incident
    revisions = [i.revision for i in published]
    assert revisions == sorted(set(revisions))  # no snapshot published twice


async def test_redelivery_does_not_open_a_second_incident() -> None:
    redis = FakeAsyncRedis()
    worker = make(redis)
    for s in warm():
        await worker.handle(s)
    spike = metric(0.2, 600)
    await worker.handle(spike)
    await worker.handle(spike)
    assert len(await incidents(redis)) == 1


async def test_a_worker_started_at_the_tail_ignores_the_backlog() -> None:
    redis = FakeAsyncRedis()
    for sig in [*warm(), metric(0.2, 600)]:
        await publish(redis, sig)
    engine = DetectionEngine.default({})
    tracker = IncidentTracker(engine.coordinator, cooldown_s=120.0)
    worker = CorrelationWorker(redis, engine, tracker, block_ms=10, start_id="$")
    await worker.ensure_group()
    assert await worker.poll_once() == 0
    assert await incidents(redis) == []
