"""Prometheus collector: pure mapping (`row_to_signal`) and the poll loop against a fake backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from inframind.common import stream_keys
from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.ingestion.prometheus import (
    MetricQuery,
    PrometheusCollector,
    row_to_signal,
    service_from_pod,
)

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
T0 = int(NOW.timestamp())


def row(pod: str = "frontend-7d9f8c6b5-abcde", values: list[Any] | None = None) -> dict[str, Any]:
    return {
        "metric": {"namespace": "default", "pod": pod},
        "values": values if values is not None else [[T0 - 30, "0.10"], [T0, "0.25"]],
    }


def prom_ok(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": "matrix", "result": rows}}


@pytest.mark.parametrize(
    ("pod", "service"),
    [
        ("frontend-7d9f8c6b5-abcde", "frontend"),
        ("recommendationservice-5c9d8f7b6d-x2k4p", "recommendationservice"),
        ("redis-cart-78746d49dc-lk9tz", "redis-cart"),
        ("kafka-0", "kafka"),
        ("standalone", "standalone"),
        ("Weird_Pod.Name", "weird-pod-name"),
    ],
)
def test_service_from_pod(pod: str, service: str) -> None:
    assert service_from_pod(pod) == service


def test_row_to_signal_maps_latest_sample() -> None:
    sig = row_to_signal("cpu_cores", row(), ingested_at=NOW)
    assert sig is not None
    assert sig.source is Source.PROMETHEUS
    assert sig.type is SignalType.METRIC
    assert sig.severity is Severity.INFO
    assert sig.service == "frontend"
    assert sig.ts == datetime.fromtimestamp(T0, tz=UTC)
    assert sig.attrs["metric"] == "cpu_cores"
    assert sig.attrs["value"] == 0.25
    assert sig.attrs["labels"] == {"namespace": "default", "pod": "frontend-7d9f8c6b5-abcde"}
    assert sig.attrs["ingested_at"] == NOW.isoformat()


@pytest.mark.parametrize("value", ["NaN", "+Inf", "-Inf", "not-a-number"])
def test_row_to_signal_skips_non_finite_values(value: str) -> None:
    assert row_to_signal("cpu_cores", row(values=[[T0, value]]), ingested_at=NOW) is None


def test_row_to_signal_skips_series_without_pod_or_samples() -> None:
    assert (
        row_to_signal("cpu_cores", {"metric": {}, "values": [[T0, "1"]]}, ingested_at=NOW) is None
    )
    assert row_to_signal("cpu_cores", row(values=[]), ingested_at=NOW) is None


QUERIES = (MetricQuery("cpu_cores", "q_cpu"), MetricQuery("memory_bytes", "q_mem"))


class FakeProm:
    """Serves canned `query_range` answers keyed by PromQL, and records the requests."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers[request.url.params["query"]]
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)


def make_collector(prom: FakeProm, redis: FakeAsyncRedis) -> PrometheusCollector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(prom), base_url="http://prom")
    return PrometheusCollector(client, redis, queries=QUERIES, interval_s=30, clock=lambda: NOW)


async def stream_signals(redis: FakeAsyncRedis) -> list[Signal]:
    entries = await redis.xrange(stream_keys.SIGNALS)
    return [Signal.model_validate_json(fields[b"data"]) for _, fields in entries]


async def test_poll_once_publishes_one_signal_per_series() -> None:
    redis = FakeAsyncRedis()
    prom = FakeProm(
        {
            "q_cpu": prom_ok([row("frontend-7d9f8c6b5-abcde"), row("cartservice-6c8f9d7b5-qwert")]),
            "q_mem": prom_ok([row("frontend-7d9f8c6b5-abcde", [[T0, "1048576"]])]),
        }
    )
    published = await make_collector(prom, redis).poll_once()
    signals = await stream_signals(redis)
    assert published == 3
    assert sorted((s.attrs["metric"], s.service) for s in signals) == [
        ("cpu_cores", "cartservice"),
        ("cpu_cores", "frontend"),
        ("memory_bytes", "frontend"),
    ]


async def test_poll_once_requests_a_lookback_window_ending_now() -> None:
    redis = FakeAsyncRedis()
    prom = FakeProm({"q_cpu": prom_ok([]), "q_mem": prom_ok([])})
    await make_collector(prom, redis).poll_once()
    params = prom.requests[0].url.params
    assert prom.requests[0].url.path == "/api/v1/query_range"
    assert float(params["end"]) == T0
    assert float(params["start"]) == T0 - 60
    assert params["step"] == "30"


async def test_repolling_the_same_sample_does_not_republish() -> None:
    redis = FakeAsyncRedis()
    prom = FakeProm({"q_cpu": prom_ok([row()]), "q_mem": prom_ok([])})
    collector = make_collector(prom, redis)
    assert await collector.poll_once() == 1
    assert await collector.poll_once() == 0
    assert len(await stream_signals(redis)) == 1


async def test_a_newer_sample_for_the_same_series_is_published() -> None:
    redis = FakeAsyncRedis()
    prom = FakeProm({"q_cpu": prom_ok([row()]), "q_mem": prom_ok([])})
    collector = make_collector(prom, redis)
    await collector.poll_once()
    prom.answers["q_cpu"] = prom_ok([row(values=[[T0 + 30, "0.40"]])])
    assert await collector.poll_once() == 1


async def test_one_failing_query_does_not_block_the_others() -> None:
    redis = FakeAsyncRedis()
    prom = FakeProm({"q_cpu": httpx.Response(503), "q_mem": prom_ok([row()])})
    assert await make_collector(prom, redis).poll_once() == 1


@pytest.mark.parametrize(
    "bad",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json={"status": "error", "errorType": "bad_data", "error": "x"}),
        httpx.Response(200, json={"status": "success"}),
    ],
)
async def test_bad_backend_answers_are_logged_not_raised(bad: httpx.Response) -> None:
    redis = FakeAsyncRedis()
    prom = FakeProm({"q_cpu": bad, "q_mem": prom_ok([])})
    assert await make_collector(prom, redis).poll_once() == 0


async def test_unreachable_backend_is_survived() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://prom")
    collector = PrometheusCollector(client, FakeAsyncRedis(), queries=QUERIES, clock=lambda: NOW)
    assert await collector.poll_once() == 0


async def test_run_stops_when_the_event_is_set() -> None:
    redis = FakeAsyncRedis()
    prom = FakeProm({"q_cpu": prom_ok([row()]), "q_mem": prom_ok([])})
    collector = make_collector(prom, redis)
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(stop))
    for _ in range(100):
        if prom.requests:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(await stream_signals(redis)) == 1
