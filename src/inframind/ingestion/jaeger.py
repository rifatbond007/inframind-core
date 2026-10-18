"""Jaeger collector: turns traces into the service graph samples and trace signals.

Per poll it emits, from traces not seen before:

* a `GraphUpdate` on `stream:graph-updates` for every cross-service parent -> child span pair
  (D21; the RCA worker owns the graph, this collector only reports samples);
* a `TRACE` Signal per error span, which names the failing operation and callee;
* one `TRACE` Signal per service with the p95 server-span latency and error count, because the
  latency-regression detector needs durations of *successful* spans too.

Traces are read only after a short settle delay, since spans of one trace arrive over time.
The exporter's own `TraceService/Export` spans are ignored: they describe the telemetry
pipeline, not the application.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError
from redis.asyncio import Redis

from inframind.common import stream_keys
from inframind.common.bus import publish, publish_graph_update
from inframind.common.collector_state import CollectorState, MemoryState, State
from inframind.common.exceptions import BusError
from inframind.common.graph_update import GraphUpdate
from inframind.common.logging import get_logger
from inframind.common.redact import redact
from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.common.time import utcnow
from inframind.ingestion.runner import run_collector

log = get_logger(__name__)

_US = 1_000_000
_SELF_INSTRUMENTATION_PREFIX = "opentelemetry.proto."
_INTERNAL_SERVICES = frozenset({"jaeger-all-in-one", "jaeger-query", "jaeger-collector"})
_HEX_ID = re.compile(r"^[0-9a-f]{1,32}$")
MAX_DESCRIPTION_CHARS = 500


@dataclass(frozen=True)
class TraceOutputs:
    """What one trace yields. `server_spans` are (service, duration_ms, is_error)."""

    updates: list[GraphUpdate] = field(default_factory=list)
    errors: list[Signal] = field(default_factory=list)
    server_spans: list[tuple[str, float, bool]] = field(default_factory=list)


def _us_to_datetime(us: int) -> datetime:
    return datetime.fromtimestamp(us // _US, tz=UTC) + timedelta(microseconds=us % _US)


def _trace_id(raw: Any) -> str | None:
    """Return a W3C 32-hex trace id (Jaeger may drop leading zeros), or None."""
    text = str(raw).lower()
    return text.zfill(32) if _HEX_ID.match(text) else None


def _tags(span: dict[str, Any]) -> dict[str, Any]:
    return {t["key"]: t.get("value") for t in span.get("tags", []) if "key" in t}


def _is_error(tags: dict[str, Any]) -> bool:
    return tags.get("error") in (True, "true") or tags.get("otel.status_code") == "ERROR"


def trace_to_outputs(trace: dict[str, Any], *, ingested_at: datetime) -> TraceOutputs:
    """Map one Jaeger trace to graph updates, error signals and server-span latencies."""
    processes: dict[str, Any] = trace.get("processes", {})
    spans = [
        s
        for s in trace.get("spans", [])
        if not str(s.get("operationName", "")).startswith(_SELF_INSTRUMENTATION_PREFIX)
    ]
    service_of: dict[str, str] = {}
    span_by_id: dict[str, dict[str, Any]] = {}
    for s in spans:
        name = processes.get(s.get("processID"), {}).get("serviceName")
        if name:
            service_of[s["spanID"]] = name
            span_by_id[s["spanID"]] = s
    trace_id = _trace_id(trace.get("traceID", ""))
    out = TraceOutputs()
    for s in spans:
        service = service_of.get(s["spanID"])
        if service is None:
            continue
        tags = _tags(s)
        start_us, duration_ms = int(s["startTime"]), s["duration"] / 1000
        parent_span = next(
            (
                span_by_id[r["spanID"]]
                for r in s.get("references", [])
                if r.get("refType") == "CHILD_OF" and r.get("spanID") in span_by_id
            ),
            None,
        )
        parent = service_of[parent_span["spanID"]] if parent_span else None
        caller = parent if parent != service else None
        if caller is not None and parent_span is not None:
            # The edge latency is what the caller observed: its client span covers network time
            # as well as the callee's processing, so a delay between services stays visible.
            observed_ms = (
                parent_span["duration"] / 1000
                if _tags(parent_span).get("span.kind") == "client"
                else duration_ms
            )
            with contextlib.suppress(ValidationError):
                out.updates.append(
                    GraphUpdate(
                        caller=caller,
                        callee=service,
                        latency_ms=observed_ms,
                        observed_at=_us_to_datetime(start_us),
                    )
                )
        error = _is_error(tags)
        kind = str(tags.get("span.kind", ""))
        if kind == "server":
            out.server_spans.append((service, duration_ms, error))
        if error:
            attrs: dict[str, Any] = {
                "operation": str(s.get("operationName", "")),
                "span_id": s["spanID"],
                "duration_ms": duration_ms,
                "kind": kind,
                "caller": caller,
                "ingested_at": ingested_at.isoformat(),
            }
            description = tags.get("otel.status_description")
            if description:
                attrs["status_description"] = redact(str(description))[:MAX_DESCRIPTION_CHARS]
            out.errors.append(
                Signal(
                    ts=_us_to_datetime(start_us),
                    source=Source.JAEGER,
                    type=SignalType.TRACE,
                    service=service,
                    severity=Severity.CRITICAL,
                    trace_id=trace_id,
                    attrs=attrs,
                )
            )
    return out


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _trace_start_us(trace: dict[str, Any]) -> int:
    return min((int(s["startTime"]) for s in trace.get("spans", [])), default=0)


class JaegerCollector:
    """Polls the Jaeger query API and publishes graph updates and trace signals.

    Each trace is published whole and only then marked seen, so a bus outage retries the trace
    on the next poll (at worst duplicating that one trace's updates) instead of losing it.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        redis: Redis,
        *,
        interval_s: int = 30,
        settle_s: int = 10,
        overlap_s: int = 30,
        initial_lookback_s: int = 120,
        limit: int = 1000,
        signals_stream: str = stream_keys.SIGNALS,
        graph_stream: str = stream_keys.GRAPH_UPDATES,
        state: State | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._client = client
        self._redis = redis
        self._interval_s = interval_s
        self._settle_us = settle_s * _US
        self._overlap_us = overlap_s * _US
        self._initial_lookback_us = initial_lookback_s * _US
        self._limit = limit
        self._signals_stream = signals_stream
        self._graph_stream = graph_stream
        self._clock = clock
        self._state: State = state or MemoryState()
        self._cursor_loaded = False
        self._cursor_us: int | None = None

    async def poll_once(self) -> int:
        """Query Jaeger once. Returns the number of items published (updates + signals)."""
        if not self._cursor_loaded:
            try:
                self._cursor_us = await self._state.load_cursor()
            except BusError as e:
                log.warning("jaeger_state_unavailable", error=str(e))
                return 0
            self._cursor_loaded = True
        now = self._clock()
        end_us = int(now.timestamp()) * _US + now.microsecond - self._settle_us
        start_us = (
            end_us - self._initial_lookback_us
            if self._cursor_us is None
            else self._cursor_us - self._overlap_us
        )
        try:
            services = await self._services()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("jaeger_services_failed", error=str(e))
            return 0
        fresh: dict[str, dict[str, Any]] = {}
        for service in services:
            try:
                traces = await self._traces(service, start_us, end_us)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                log.warning("jaeger_query_failed", service=service, error=str(e))
                continue
            for trace in traces:
                tid = str(trace.get("traceID", ""))
                try:
                    if tid and not await self._state.is_seen(tid):
                        fresh.setdefault(tid, trace)
                except BusError as e:
                    log.warning("jaeger_state_unavailable", error=str(e))
                    return 0
        published, server_spans, complete = await self._publish_traces(fresh, now)
        published += await self._publish_stats(server_spans, now, end_us) if complete else 0
        if complete:
            self._cursor_us = end_us
            try:
                await self._state.save_cursor(end_us)
            except BusError as e:
                log.warning("jaeger_cursor_save_failed", error=str(e))
        return published

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set. Never raises on backend or bus failures."""
        log.info("collector_started", backend="jaeger", interval_s=self._interval_s)
        while not stop.is_set():
            await self.poll_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)

    async def _services(self) -> list[str]:
        resp = await self._client.get("/api/services")
        resp.raise_for_status()
        names = resp.json().get("data") or []
        return sorted(n for n in names if n not in _INTERNAL_SERVICES)

    async def _traces(self, service: str, start_us: int, end_us: int) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "service": service,
            "start": start_us,
            "end": end_us,
            "limit": self._limit,
        }
        resp = await self._client.get("/api/traces", params=params)
        resp.raise_for_status()
        traces: list[dict[str, Any]] = resp.json().get("data") or []
        if len(traces) >= self._limit:
            log.warning("jaeger_result_truncated", service=service, limit=self._limit)
        return traces

    async def _publish_traces(
        self, fresh: dict[str, dict[str, Any]], now: datetime
    ) -> tuple[int, list[tuple[str, float, bool]], bool]:
        """Publish new traces oldest first. Returns (published, server spans, bus_ok)."""
        published = 0
        server_spans: list[tuple[str, float, bool]] = []
        for tid, trace in sorted(fresh.items(), key=lambda kv: (_trace_start_us(kv[1]), kv[0])):
            outputs = trace_to_outputs(trace, ingested_at=now)
            try:
                for update in sorted(
                    outputs.updates, key=lambda u: (u.observed_at, u.caller, u.callee)
                ):
                    await publish_graph_update(self._redis, update, stream=self._graph_stream)
                    published += 1
                for signal in sorted(outputs.errors, key=lambda s: (s.ts, s.attrs["span_id"])):
                    if await publish(self._redis, signal, stream=self._signals_stream) is not None:
                        published += 1
                await self._state.mark_seen(tid)
            except BusError as e:
                log.warning("jaeger_publish_failed", trace_id=tid, error=str(e))
                return published, server_spans, False
            server_spans.extend(outputs.server_spans)
        return published, server_spans, True

    async def _publish_stats(
        self, server_spans: list[tuple[str, float, bool]], now: datetime, end_us: int
    ) -> int:
        by_service: dict[str, list[tuple[float, bool]]] = defaultdict(list)
        for service, duration_ms, error in server_spans:
            by_service[service].append((duration_ms, error))
        published = 0
        for service in sorted(by_service):
            samples = by_service[service]
            signal = Signal(
                ts=_us_to_datetime(end_us),
                source=Source.JAEGER,
                type=SignalType.TRACE,
                service=service,
                attrs={
                    "metric": "span_latency",
                    "p95_ms": _p95([d for d, _ in samples]),
                    "span_count": len(samples),
                    "error_count": sum(1 for _, e in samples if e),
                    "ingested_at": now.isoformat(),
                },
            )
            try:
                if await publish(self._redis, signal, stream=self._signals_stream) is not None:
                    published += 1
            except BusError as e:
                log.warning("jaeger_publish_failed", service=service, error=str(e))
                break
        return published


async def amain() -> None:
    """Run the collector until SIGINT/SIGTERM. Reads JAEGER_URL, REDIS_URL, JAEGER_INTERVAL_S."""
    await run_collector(
        lambda client, redis, interval_s: JaegerCollector(
            client, redis, interval_s=interval_s, state=CollectorState(redis, "jaeger")
        ),
        url_env="JAEGER_URL",
        default_url="http://localhost:30686",
        interval_env="JAEGER_INTERVAL_S",
        timeout_s=20.0,
    )


if __name__ == "__main__":
    asyncio.run(amain())
