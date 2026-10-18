"""Prometheus collector: pulls `query_range` results and publishes them as `Signal`s.

Online Boutique services are gRPC and expose no per-service request metrics, so the default
queries use cAdvisor and kube-state-metrics, grouped by pod. The service name is derived from
the pod name (`service_from_pod`). Request errors and latency come from Loki and Jaeger instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import signal as os_signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from redis.asyncio import Redis

from inframind.common import stream_keys
from inframind.common.bus import publish
from inframind.common.exceptions import BusError
from inframind.common.logging import configure_logging, get_logger
from inframind.common.signal import Severity, Signal, SignalType, Source, service_from_pod
from inframind.common.time import utcnow

log = get_logger(__name__)

_NAMESPACE = "default"


@dataclass(frozen=True)
class MetricQuery:
    """One PromQL query; `name` is the logical metric name stored in `Signal.attrs["metric"]`."""

    name: str
    promql: str


DEFAULT_QUERIES: tuple[MetricQuery, ...] = (
    MetricQuery(
        "cpu_cores",
        f'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{{namespace="{_NAMESPACE}",'
        'container!="",container!="POD"}[1m]))',
    ),
    MetricQuery(
        "memory_working_set_bytes",
        f'sum by (namespace, pod) (container_memory_working_set_bytes{{namespace="{_NAMESPACE}",'
        'container!="",container!="POD"})',
    ),
    MetricQuery(
        "container_restarts_total",
        f'sum by (namespace, pod) (kube_pod_container_status_restarts_total{{namespace="{_NAMESPACE}"}})',
    ),
)


def row_to_signal(name: str, row: dict[str, Any], *, ingested_at: datetime) -> Signal | None:
    """Map the latest sample of one `query_range` series to a `Signal`.

    Returns None when the series has no pod label, no samples, or a non-finite latest value.
    """
    labels: dict[str, str] = row.get("metric", {})
    pod = labels.get("pod")
    values = row.get("values") or []
    if not pod or not values:
        return None
    ts_unix, value_str = values[-1]
    try:
        value = float(value_str)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return Signal(
        ts=datetime.fromtimestamp(float(ts_unix), tz=UTC),
        source=Source.PROMETHEUS,
        type=SignalType.METRIC,
        service=service_from_pod(pod),
        severity=Severity.INFO,
        attrs={
            "metric": name,
            "value": value,
            "labels": {k: v for k, v in labels.items() if k != "__name__"},
            "ingested_at": ingested_at.isoformat(),
        },
    )


class PrometheusCollector:
    """Polls Prometheus on an interval and publishes new samples to `stream:signals`.

    A per-series cursor keeps a sample from being published twice when consecutive lookback
    windows overlap, so the detector sees each observation once.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        redis: Redis,
        *,
        queries: tuple[MetricQuery, ...] = DEFAULT_QUERIES,
        interval_s: int = 30,
        stream: str = stream_keys.SIGNALS,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._client = client
        self._redis = redis
        self._queries = queries
        self._interval_s = interval_s
        self._stream = stream
        self._clock = clock
        self._cursor: dict[tuple[str, str], datetime] = {}

    async def poll_once(self) -> int:
        """Run every query once. Returns the number of signals published."""
        now = self._clock()
        published = 0
        for query in self._queries:
            try:
                rows = await self._fetch(query, now)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                log.warning("prometheus_query_failed", metric=query.name, error=str(e))
                continue
            for row in rows:
                published += await self._publish_row(query.name, row, now)
        return published

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set. Never raises on backend or bus failures."""
        log.info("collector_started", backend="prometheus", interval_s=self._interval_s)
        while not stop.is_set():
            await self.poll_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)

    async def _fetch(self, query: MetricQuery, now: datetime) -> list[dict[str, Any]]:
        end = now.timestamp()
        params: dict[str, str | float] = {
            "query": query.promql,
            "start": end - 2 * self._interval_s,
            "end": end,
            "step": str(self._interval_s),
        }
        resp = await self._client.get("/api/v1/query_range", params=params)
        resp.raise_for_status()
        body = resp.json()
        if body["status"] != "success":
            raise ValueError(f"prometheus status={body['status']} error={body.get('error')}")
        return list(body["data"]["result"])

    async def _publish_row(self, name: str, row: dict[str, Any], now: datetime) -> int:
        signal = row_to_signal(name, row, ingested_at=now)
        if signal is None:
            return 0
        key = (name, signal.attrs["labels"]["pod"])
        last = self._cursor.get(key)
        if last is not None and signal.ts <= last:
            return 0
        try:
            await publish(self._redis, signal, stream=self._stream)
        except BusError as e:
            log.warning("prometheus_publish_failed", metric=name, error=str(e))
            return 0
        self._cursor[key] = signal.ts
        return 1


async def amain() -> None:
    """Run the collector until SIGINT/SIGTERM. Reads PROMETHEUS_URL, REDIS_URL, PROM_INTERVAL_S."""
    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    base_url = os.environ.get("PROMETHEUS_URL", "http://localhost:30090")
    interval_s = int(os.environ.get("PROM_INTERVAL_S", "30"))
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(10.0)) as client:
        try:
            await PrometheusCollector(client, redis, interval_s=interval_s).run(stop)
        finally:
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(amain())
