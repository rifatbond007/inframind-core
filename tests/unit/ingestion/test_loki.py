"""Loki collector: pure line classification/mapping and the poll loop against a fake backend."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from inframind.common import stream_keys
from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.ingestion.loki import LokiCollector, classify_line, entry_to_signal

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
NOW_NS = int(NOW.timestamp()) * 1_000_000_000
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
LABELS = {"app": "frontend", "pod": "frontend-5c744dd6bb-jfpxt", "container": "server"}


def jline(**fields: Any) -> str:
    return json.dumps({"message": "boom", "timestamp": "2026-10-19T12:00:00Z", **fields})


@pytest.mark.parametrize(
    ("line", "level"),
    [
        (jline(severity="error"), "error"),
        (jline(severity="ERROR"), "error"),
        (jline(severity="warning"), "warning"),
        (jline(level="warn"), "warning"),
        (jline(severity="fatal"), "error"),
        (jline(severity="debug", message="error budget ok"), None),
        (jline(severity="info", message="no error here"), None),
        ("2026-10-19 ERROR payment failed", "error"),
        ("WARNING: slow upstream", "warning"),
        ("Traceback (most recent call last):", "error"),
        ("java.lang.NullPointerException", "error"),
        ("Checking CartService Health", None),
        ("", None),
    ],
)
def test_classify_line(line: str, level: str | None) -> None:
    assert classify_line(line).level == level


def test_classify_json_appends_the_error_field_and_grpc_code() -> None:
    parsed = classify_line(
        jline(
            severity="error",
            message="request error",
            error="could not retrieve product: rpc error: code = NotFound desc = no product",
            session="user-session-id",
        )
    )
    assert parsed.message == (
        "request error: could not retrieve product: rpc error: code = NotFound desc = no product"
    )
    assert parsed.grpc_code == "NotFound"


def test_session_ids_never_reach_the_signal() -> None:
    sig = entry_to_signal(
        LABELS, str(NOW_NS), jline(severity="error", session="user-session-id"), ingested_at=NOW
    )
    assert sig is not None
    assert "user-session-id" not in sig.model_dump_json()


def test_classify_json_extracts_message_status_and_trace() -> None:
    parsed = classify_line(
        jline(
            severity="error", message="upstream failed", **{"http.resp.status": 503}, trace_id=TRACE
        )
    )
    assert parsed.message == "upstream failed"
    assert parsed.http_status == 503
    assert parsed.trace_id == TRACE


@pytest.mark.parametrize("bad", ["not-hex", TRACE.upper(), TRACE[:-1], 123])
def test_classify_ignores_invalid_trace_ids(bad: Any) -> None:
    assert classify_line(jline(severity="error", trace_id=bad)).trace_id is None


def test_classify_text_line_finds_a_trace_id() -> None:
    assert classify_line(f"ERROR failed trace_id={TRACE} retrying").trace_id == TRACE


def test_entry_to_signal_maps_an_error_line() -> None:
    line = jline(severity="error", message="upstream failed", **{"http.resp.status": 503})
    sig = entry_to_signal(LABELS, str(NOW_NS + 123_456_789), line, ingested_at=NOW)
    assert sig is not None
    assert sig.source is Source.LOKI
    assert sig.type is SignalType.LOG
    assert sig.service == "frontend"
    assert sig.severity is Severity.CRITICAL
    assert sig.ts == datetime(2026, 10, 19, 12, 0, 0, 123456, tzinfo=UTC)
    assert sig.attrs["level"] == "error"
    assert sig.attrs["message"] == "upstream failed"
    assert sig.attrs["http_status"] == 503
    assert "grpc_code" not in sig.attrs
    assert sig.attrs["pod"] == "frontend-5c744dd6bb-jfpxt"
    assert sig.attrs["container"] == "server"
    assert sig.attrs["ingested_at"] == NOW.isoformat()
    assert "raw" not in sig.attrs


def test_entry_to_signal_warning_severity() -> None:
    sig = entry_to_signal(LABELS, str(NOW_NS), jline(severity="warn"), ingested_at=NOW)
    assert sig is not None
    assert sig.severity is Severity.WARNING


def test_entry_to_signal_skips_non_error_lines() -> None:
    assert entry_to_signal(LABELS, str(NOW_NS), jline(severity="debug"), ingested_at=NOW) is None


def test_message_is_truncated() -> None:
    sig = entry_to_signal(LABELS, str(NOW_NS), "ERROR " + "x" * 5000, ingested_at=NOW)
    assert sig is not None
    assert len(sig.attrs["message"]) <= 500


def test_service_falls_back_to_pod_name_then_unknown() -> None:
    labels = {"pod": "cartservice-7848b87fc5-g2rgx"}
    sig = entry_to_signal(labels, str(NOW_NS), "ERROR x", ingested_at=NOW)
    assert sig is not None
    assert sig.service == "cartservice"
    bare = entry_to_signal({}, str(NOW_NS), "ERROR x", ingested_at=NOW)
    assert bare is not None
    assert not bare.is_routable


def test_bad_timestamp_is_skipped() -> None:
    assert entry_to_signal(LABELS, "not-ns", "ERROR x", ingested_at=NOW) is None


def loki_ok(*streams: tuple[dict[str, str], list[tuple[int, str]]]) -> dict[str, Any]:
    result = [
        {"stream": labels, "values": [[str(ts), line] for ts, line in values]}
        for labels, values in streams
    ]
    return {"status": "success", "data": {"resultType": "streams", "result": result}}


class FakeLoki:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.answer, httpx.Response):
            return self.answer
        return httpx.Response(200, json=self.answer)


def make(loki: FakeLoki, redis: FakeAsyncRedis, **kw: Any) -> LokiCollector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(loki), base_url="http://loki")
    return LokiCollector(client, redis, interval_s=30, clock=lambda: NOW, **kw)


async def stream_signals(redis: FakeAsyncRedis) -> list[Signal]:
    entries = await redis.xrange(stream_keys.SIGNALS)
    return [Signal.model_validate_json(f[b"data"]) for _, f in entries]


ERR = jline(severity="error")


async def test_poll_once_publishes_only_error_and_warning_lines() -> None:
    redis = FakeAsyncRedis()
    cart = {"app": "cartservice", "pod": "cartservice-7848b87fc5-g2rgx"}
    loki = FakeLoki(
        loki_ok(
            (LABELS, [(NOW_NS - 2, ERR), (NOW_NS - 1, jline(severity="debug", message="error?"))]),
            (cart, [(NOW_NS, "WARNING: slow")]),
        )
    )
    published = await make(loki, redis).poll_once()
    sigs = await stream_signals(redis)
    assert published == 2
    assert sorted((s.service, s.attrs["level"]) for s in sigs) == [
        ("cartservice", "warning"),
        ("frontend", "error"),
    ]


async def test_first_poll_requests_the_initial_lookback_window() -> None:
    loki = FakeLoki(loki_ok())
    await make(loki, FakeAsyncRedis(), initial_lookback_s=60).poll_once()
    req = loki.requests[0]
    params = req.url.params
    assert req.url.path == "/loki/api/v1/query_range"
    assert int(params["end"]) == NOW_NS
    assert int(params["start"]) == NOW_NS - 60 * 1_000_000_000
    assert params["direction"] == "forward"
    assert params["limit"] == "1000"
    assert 'namespace="default"' in params["query"]


async def test_overlapping_windows_do_not_republish_a_line() -> None:
    redis = FakeAsyncRedis()
    loki = FakeLoki(loki_ok((LABELS, [(NOW_NS - 5_000_000_000, ERR)])))
    collector = make(loki, redis, overlap_s=10)
    assert await collector.poll_once() == 1
    assert await collector.poll_once() == 0
    assert len(await stream_signals(redis)) == 1
    second_start = int(loki.requests[1].url.params["start"])
    assert second_start == NOW_NS - 5_000_000_000 - 10 * 1_000_000_000


async def test_a_late_arriving_older_line_inside_the_overlap_is_still_published() -> None:
    redis = FakeAsyncRedis()
    loki = FakeLoki(loki_ok((LABELS, [(NOW_NS - 1_000_000_000, ERR)])))
    collector = make(loki, redis, overlap_s=10)
    await collector.poll_once()
    late = jline(severity="error", message="late")
    loki.answer = loki_ok((LABELS, [(NOW_NS - 3_000_000_000, late)]))
    assert await collector.poll_once() == 1


async def test_identical_lines_at_the_same_instant_from_different_pods_both_publish() -> None:
    redis = FakeAsyncRedis()
    other = {**LABELS, "pod": "frontend-5c744dd6bb-zzzzz"}
    loki = FakeLoki(loki_ok((LABELS, [(NOW_NS, ERR)]), (other, [(NOW_NS, ERR)])))
    assert await make(loki, redis).poll_once() == 2


@pytest.mark.parametrize(
    "bad",
    [
        httpx.Response(503),
        httpx.Response(200, content=b"<html>"),
        httpx.Response(200, json={"status": "error", "error": "x"}),
        httpx.Response(200, json={"status": "success"}),
    ],
)
async def test_bad_backend_answers_are_survived(bad: httpx.Response) -> None:
    assert await make(FakeLoki(bad), FakeAsyncRedis()).poll_once() == 0


async def test_unreachable_backend_is_survived() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://loki")
    assert await LokiCollector(client, FakeAsyncRedis(), clock=lambda: NOW).poll_once() == 0


async def test_malformed_entries_do_not_poison_the_batch() -> None:
    redis = FakeAsyncRedis()
    answer = loki_ok((LABELS, [(NOW_NS, ERR)]))
    answer["data"]["result"][0]["values"].insert(0, ["only-one-field"])
    assert await make(FakeLoki(answer), redis).poll_once() == 1


async def test_run_stops_when_the_event_is_set() -> None:
    redis = FakeAsyncRedis()
    loki = FakeLoki(loki_ok((LABELS, [(NOW_NS, ERR)])))
    collector = make(loki, redis)
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(stop))
    for _ in range(100):
        if loki.requests:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(await stream_signals(redis)) == 1
