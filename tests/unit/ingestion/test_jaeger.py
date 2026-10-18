"""Jaeger collector: pure trace mapping and the poll loop against a fake query API."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from inframind.common import stream_keys
from inframind.common.collector_state import CollectorState
from inframind.common.graph_update import GraphUpdate
from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.ingestion.jaeger import JaegerCollector, trace_to_outputs

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
NOW_US = int(NOW.timestamp()) * 1_000_000
TID = "4bf92f3577b34da6a3ce929d0e0e4736"
PROCS = {"p1": "frontend", "p2": "productcatalogservice", "p3": "recommendationservice"}


def sp(
    sid: str,
    proc: str,
    op: str = "op",
    *,
    parent: str | None = None,
    start: int = 0,
    dur_ms: float = 10,
    kind: str = "server",
    error: bool = False,
    desc: str | None = None,
    ref_type: str = "CHILD_OF",
    trace_id: str = TID,
) -> dict[str, Any]:
    tags = [{"key": "span.kind", "type": "string", "value": kind}]
    if error:
        tags += [
            {"key": "error", "type": "bool", "value": True},
            {"key": "otel.status_code", "type": "string", "value": "ERROR"},
        ]
    if desc:
        tags.append({"key": "otel.status_description", "type": "string", "value": desc})
    refs = [{"refType": ref_type, "traceID": trace_id, "spanID": parent}] if parent else []
    return {
        "traceID": trace_id,
        "spanID": sid,
        "operationName": op,
        "references": refs,
        "startTime": NOW_US - 5_000_000 + start,
        "duration": int(dur_ms * 1000),
        "tags": tags,
        "processID": proc,
    }


def tr(*spans: dict[str, Any], trace_id: str = TID) -> dict[str, Any]:
    return {
        "traceID": trace_id,
        "spans": list(spans),
        "processes": {k: {"serviceName": v} for k, v in PROCS.items()},
    }


def out(trace: dict[str, Any]) -> Any:
    return trace_to_outputs(trace, ingested_at=NOW)


def test_cross_service_child_span_becomes_a_graph_update() -> None:
    trace = tr(
        sp("a", "p1", "GET", kind="server"),
        sp("b", "p1", "GetProduct", parent="a", kind="client", start=1_000),
        sp("c", "p2", "GetProduct", parent="b", kind="server", start=2_000, dur_ms=7.5),
    )
    [update] = out(trace).updates
    assert (update.caller, update.callee) == ("frontend", "productcatalogservice")
    assert update.latency_ms == 7.5
    assert update.observed_at == datetime.fromtimestamp((NOW_US - 5_000_000 + 2_000) / 1e6, tz=UTC)


def test_same_service_parent_child_is_not_an_edge() -> None:
    trace = tr(sp("a", "p1"), sp("b", "p1", parent="a"))
    assert out(trace).updates == []


def test_missing_parent_and_follows_from_are_ignored() -> None:
    trace = tr(
        sp("a", "p1"),
        sp("orphan", "p2", parent="not-in-trace"),
        sp("async", "p3", parent="a", ref_type="FOLLOWS_FROM"),
    )
    assert out(trace).updates == []


def test_error_span_becomes_a_critical_trace_signal() -> None:
    trace = tr(
        sp("a", "p1", "GET", kind="server"),
        sp(
            "b",
            "p1",
            "hipstershop.ProductCatalogService/GetProduct",
            parent="a",
            kind="client",
            error=True,
            desc="no product with ID NOPE-9",
            dur_ms=3,
        ),
    )
    [sig] = out(trace).errors
    assert sig.source is Source.JAEGER
    assert sig.type is SignalType.TRACE
    assert sig.severity is Severity.CRITICAL
    assert sig.service == "frontend"
    assert sig.trace_id == TID
    assert sig.attrs["operation"] == "hipstershop.ProductCatalogService/GetProduct"
    assert sig.attrs["span_id"] == "b"
    assert sig.attrs["duration_ms"] == 3.0
    assert sig.attrs["kind"] == "client"
    assert sig.attrs["status_description"] == "no product with ID NOPE-9"
    assert sig.attrs["ingested_at"] == NOW.isoformat()


def test_error_span_records_the_calling_service() -> None:
    trace = tr(sp("a", "p1"), sp("c", "p2", parent="a", error=True))
    [sig] = out(trace).errors
    assert sig.service == "productcatalogservice"
    assert sig.attrs["caller"] == "frontend"
    [root] = out(tr(sp("a", "p1", error=True))).errors
    assert root.attrs["caller"] is None


def test_ok_spans_produce_no_error_signal() -> None:
    assert out(tr(sp("a", "p1"), sp("b", "p2", parent="a"))).errors == []


def test_only_server_spans_feed_latency_stats() -> None:
    trace = tr(
        sp("a", "p1", kind="server", dur_ms=20),
        sp("b", "p1", parent="a", kind="client", dur_ms=15),
        sp("c", "p2", parent="b", kind="server", dur_ms=5, error=True),
        sp("d", "p1", parent="a", kind="internal", dur_ms=1),
    )
    assert sorted(out(trace).server_spans) == [
        ("frontend", 20.0, False),
        ("productcatalogservice", 5.0, True),
    ]


def test_exporter_self_instrumentation_spans_are_ignored() -> None:
    noise = sp(
        "x",
        "p1",
        "opentelemetry.proto.collector.trace.v1.TraceService/Export",
        kind="client",
        error=True,
    )
    result = out(tr(noise))
    assert (result.updates, result.errors, result.server_spans) == ([], [], [])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (TID, TID),
        ("a3ce929d0e0e4736", "0000000000000000a3ce929d0e0e4736"),
        ("zzzz", None),
        ("", None),
    ],
)
def test_trace_ids_are_normalised_to_w3c(raw: str, expected: str | None) -> None:
    [sig] = out(tr(sp("a", "p1", error=True, trace_id=raw), trace_id=raw)).errors
    assert sig.trace_id == expected


def test_span_with_unknown_process_is_skipped() -> None:
    trace = tr(sp("a", "p1"), sp("b", "p99", parent="a", error=True))
    result = out(trace)
    assert result.updates == [] and result.errors == []


class FakeJaeger:
    """Serves `/api/services` and per-service `/api/traces`, recording the requests."""

    def __init__(
        self, services: list[str], traces: dict[str, list[dict[str, Any]]] | None = None
    ) -> None:
        self.services = services
        self.traces = traces or {}
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/services":
            if "/api/services" in self.fail:
                return httpx.Response(self.fail["/api/services"])
            return httpx.Response(200, json={"data": self.services})
        service = request.url.params["service"]
        if service in self.fail:
            return httpx.Response(self.fail[service])
        return httpx.Response(200, json={"data": self.traces.get(service, [])})

    def trace_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/api/traces"]


def make(jaeger: FakeJaeger, redis: FakeAsyncRedis, **kw: Any) -> JaegerCollector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(jaeger), base_url="http://jaeger")
    return JaegerCollector(client, redis, interval_s=30, clock=lambda: NOW, **kw)


async def read_signals(redis: FakeAsyncRedis) -> list[Signal]:
    entries = await redis.xrange(stream_keys.SIGNALS)
    return [Signal.model_validate_json(f[b"data"]) for _, f in entries]


async def read_updates(redis: FakeAsyncRedis) -> list[GraphUpdate]:
    entries = await redis.xrange(stream_keys.GRAPH_UPDATES)
    return [GraphUpdate.model_validate_json(f[b"data"]) for _, f in entries]


GOOD_TRACE = tr(
    sp("a", "p1", "GET", kind="server", dur_ms=20),
    sp("b", "p1", "GetProduct", parent="a", kind="client", start=1_000, dur_ms=9),
    sp("c", "p2", "GetProduct", parent="b", kind="server", start=2_000, dur_ms=5),
)
ERR_TRACE = tr(
    sp("a", "p1", "GET", kind="server", dur_ms=30, error=True),
    sp("b", "p1", "GetProduct", parent="a", kind="client", error=True, desc="boom"),
    trace_id="1" * 32,
)


async def test_poll_publishes_updates_errors_and_latency_stats() -> None:
    redis = FakeAsyncRedis()
    jaeger = FakeJaeger(
        ["frontend", "productcatalogservice"], {"frontend": [GOOD_TRACE, ERR_TRACE]}
    )
    published = await make(jaeger, redis).poll_once()
    updates = await read_updates(redis)
    signals = await read_signals(redis)
    assert [(u.caller, u.callee) for u in updates] == [("frontend", "productcatalogservice")]
    errors = [s for s in signals if s.attrs.get("metric") != "span_latency"]
    stats = {s.service: s for s in signals if s.attrs.get("metric") == "span_latency"}
    assert len(errors) == 2
    assert set(stats) == {"frontend", "productcatalogservice"}
    assert stats["frontend"].attrs["span_count"] == 2
    assert stats["frontend"].attrs["error_count"] == 1
    assert stats["frontend"].attrs["p95_ms"] == 30.0
    assert stats["productcatalogservice"].attrs["p95_ms"] == 5.0
    assert stats["frontend"].severity is Severity.INFO
    assert published == len(updates) + len(signals) == 1 + 2 + 2


async def test_graph_updates_are_published_in_a_deterministic_order() -> None:
    t1 = tr(
        sp("a", "p1"),
        sp("b", "p3", parent="a", start=3_000),
        sp("c", "p2", parent="a", start=1_000),
        trace_id="2" * 32,
    )
    runs = []
    for services in (["frontend"], ["frontend"]):
        redis = FakeAsyncRedis()
        await make(FakeJaeger(services, {"frontend": [t1]}), redis).poll_once()
        runs.append([(u.caller, u.callee, u.observed_at) for u in await read_updates(redis)])
    assert runs[0] == runs[1]
    assert [r[1] for r in runs[0]] == ["productcatalogservice", "recommendationservice"]


async def test_jaeger_own_service_is_not_queried() -> None:
    jaeger = FakeJaeger(["frontend", "jaeger-all-in-one", "jaeger-query"])
    await make(jaeger, FakeAsyncRedis()).poll_once()
    assert [r.url.params["service"] for r in jaeger.trace_requests()] == ["frontend"]


async def test_query_window_ends_after_the_settle_delay_and_uses_microseconds() -> None:
    jaeger = FakeJaeger(["frontend"])
    await make(jaeger, FakeAsyncRedis(), settle_s=10, initial_lookback_s=120, limit=500).poll_once()
    params = jaeger.trace_requests()[0].url.params
    assert int(params["end"]) == NOW_US - 10 * 1_000_000
    assert int(params["start"]) == NOW_US - 130 * 1_000_000
    assert params["limit"] == "500"


async def test_a_trace_seen_under_two_services_is_processed_once() -> None:
    redis = FakeAsyncRedis()
    jaeger = FakeJaeger(
        ["frontend", "productcatalogservice"],
        {"frontend": [GOOD_TRACE], "productcatalogservice": [GOOD_TRACE]},
    )
    await make(jaeger, redis).poll_once()
    assert len(await read_updates(redis)) == 1


async def test_repolling_does_not_republish_and_overlaps_the_previous_window() -> None:
    redis = FakeAsyncRedis()
    jaeger = FakeJaeger(["frontend"], {"frontend": [GOOD_TRACE]})
    collector = make(jaeger, redis, settle_s=10, overlap_s=30)
    assert await collector.poll_once() > 0
    assert await collector.poll_once() == 0
    assert len(await read_updates(redis)) == 1
    first_end = int(jaeger.trace_requests()[0].url.params["end"])
    assert int(jaeger.trace_requests()[1].url.params["start"]) == first_end - 30 * 1_000_000


async def test_one_failing_service_does_not_block_the_others() -> None:
    redis = FakeAsyncRedis()
    jaeger = FakeJaeger(
        ["frontend", "productcatalogservice"], {"productcatalogservice": [GOOD_TRACE]}
    )
    jaeger.fail["frontend"] = 503
    assert await make(jaeger, redis).poll_once() > 0


async def test_services_listing_failure_is_survived() -> None:
    jaeger = FakeJaeger(["frontend"])
    jaeger.fail["/api/services"] = 500
    assert await make(jaeger, FakeAsyncRedis()).poll_once() == 0


async def test_unreachable_backend_is_survived() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://jaeger")
    assert await JaegerCollector(client, FakeAsyncRedis(), clock=lambda: NOW).poll_once() == 0


async def test_a_bus_outage_loses_nothing_because_traces_are_retried() -> None:
    class Flaky(FakeAsyncRedis):
        down = True

        async def xadd(self, *args: Any, **kwargs: Any) -> Any:
            from redis import exceptions

            if self.down:
                raise exceptions.ConnectionError("down")
            return await super().xadd(*args, **kwargs)

    redis = Flaky()
    jaeger = FakeJaeger(["frontend"], {"frontend": [GOOD_TRACE]})
    collector = make(jaeger, redis)
    assert await collector.poll_once() == 0
    redis.down = False
    assert await collector.poll_once() > 0
    assert len(await read_updates(redis)) == 1


async def test_run_stops_when_the_event_is_set() -> None:
    redis = FakeAsyncRedis()
    jaeger = FakeJaeger(["frontend"], {"frontend": [GOOD_TRACE]})
    stop = asyncio.Event()
    task = asyncio.create_task(make(jaeger, redis).run(stop))
    for _ in range(100):
        if jaeger.trace_requests():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(await read_updates(redis)) == 1


async def test_restarted_collector_does_not_republish() -> None:
    redis = FakeAsyncRedis()
    jaeger = FakeJaeger(["frontend"], {"frontend": [GOOD_TRACE, ERR_TRACE]})
    first = make(jaeger, redis, state=CollectorState(redis, "jaeger"))
    assert await first.poll_once() > 0
    before = (await redis.xlen(stream_keys.SIGNALS), await redis.xlen(stream_keys.GRAPH_UPDATES))
    restarted = make(jaeger, redis, state=CollectorState(redis, "jaeger"))
    assert await restarted.poll_once() == 0
    assert (
        await redis.xlen(stream_keys.SIGNALS),
        await redis.xlen(stream_keys.GRAPH_UPDATES),
    ) == before
