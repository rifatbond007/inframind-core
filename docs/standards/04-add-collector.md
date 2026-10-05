# 4. Add a collector — Prometheus / Loki / Jaeger / Alertmanager
> standards · Python conventions · InfraMind. Pairs with the `add-collector` skill in `.claude/skills/add-collector/SKILL.md`. This file is the rationale and the worked example.

## 4.1 The shape every collector has

```
src/inframind/ingestion/<backend>.py
```

```python
from __future__ import annotations

from inframind.common.logging import get_logger
from inframind.common.signal import Signal, Source, SignalType
from inframind.common.stream_keys import SIGNALS_STREAM


class <Backend>Collector:
    """Pulls <backend> signals and publishes them as `Signal` to the bus."""

    def __init__(self, url: str, interval_s: int, services: list[str]) -> None:
        self._url = url
        self._interval_s = interval_s
        self._services = services
        self.log = get_logger(__name__)

    async def run(self) -> None: ...
    def _to_signal(self, raw: <BackendType>) -> Signal: ...
    def _publish(self, signal: Signal) -> None: ...
```

## 4.2 Three methods, three responsibilities

- **`async def run()`** — main loop. Pulls on an interval, calls `_to_signal` per payload,
  publishes. Catches at the network boundary; logs with context; never crashes the loop.
- **`def _to_signal(raw)`** — the mapping. Pure — takes backend shape, returns `Signal`. The
  bus consumer never sees anything else.
- **`def _publish(self, signal: Signal)`** — Redis Streams `XADD stream:signals * <fields>`.
  Idempotent on `signal.id`.

A collector never calls `detection`, never reaches for a global state, never imports anything
from `src/inframind/` other than `common/`.

## 4.3 Idempotent publish

The bus consumer dedups on `signal.id`. A re-publish of the same id is a no-op. This is
important because collectors re-publish on retry, and the network re-publishes on reconnect.

```python
def _publish(self, signal: Signal) -> None:
    payload = signal.model_dump_json()
    self._redis.xadd(SIGNALS_STREAM, {"signal": payload}, maxlen=100_000, approximate=True)
```

The `maxlen` is a soft cap that keeps the stream from growing without limit during a quiet
incident window. Past that limit, old signals are trimmed by Redis.

## 4.4 Worked example: the Prometheus collector

```python
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from inframind.common.logging import get_logger
from inframind.common.redis_client import get_redis
from inframind.common.signal import Signal, Source, SignalType
from inframind.common.stream_keys import SIGNALS_STREAM
from inframind.common.time import utcnow


class PrometheusCollector:
    """Pulls Prometheus query_range results and publishes them as `Signal`."""

    def __init__(self, url: str, services: list[str], interval_s: int = 15) -> None:
        self._url = url.rstrip("/")
        self._services = services
        self._interval_s = interval_s
        self._redis = get_redis()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.log = get_logger(__name__)

    async def run(self) -> None:
        self.log.info("collector_started", backend="prometheus", services=self._services)
        while True:
            try:
                rows = await self._fetch_range()
                for raw in rows:
                    self._publish(self._to_signal(raw))
            except httpx.HTTPError as e:
                self.log.warning(
                    "prometheus_query_failed",
                    error=str(e),
                    url=self._url,
                    next_retry_s=self._interval_s,
                )
            await asyncio.sleep(self._interval_s)

    async def _fetch_range(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for service in self._services:
            query = f'sum by(code) (rate(http_requests_total{{service="{service}"}}[1m]))'
            params = {"query": query, "start": ..., "end": ..., "step": "15s"}
            resp = await self._client.get(f"{self._url}/api/v1/query_range", params=params)
            resp.raise_for_status()
            out.extend(resp.json()["data"]["result"])
        return out

    def _to_signal(self, raw: dict[str, Any]) -> Signal:
        metric = raw["metric"]
        ts_unix, value_str = raw["values"][-1]
        return Signal(
            ts=datetime.fromtimestamp(ts_unix, tz=timezone.utc),
            source=Source.PROMETHEUS,
            type=SignalType.METRIC,
            service=_normalise_service(metric["service"]),
            severity=_severity_for_code(metric.get("code", "")),
            attrs={
                "metric": metric["__name__"],
                "value": float(value_str),
                "labels": {k: v for k, v in metric.items() if k != "__name__"},
                "ingested_at": utcnow().isoformat(),
            },
        )

    def _publish(self, signal: Signal) -> None:
        self._redis.xadd(
            SIGNALS_STREAM,
            {"signal": signal.model_dump_json()},
            maxlen=100_000,
            approximate=True,
        )
```

## 4.5 The collector discipline (and why)

- **One collector per backend.** Multiple collectors for the same backend are merged into one
  with an internal list of services or queries. Splitting by service hurts because retries and
  back-off are easier to coordinate when one loop owns them.
- **Pull, not push** — for Prometheus, Loki, Jaeger / OTel. The Alertmanager webhook is the only
  push ingress (because Alertmanager has no pull API worth using).
- **`_to_signal` is pure.** It must not touch the network. If it does, the test is harder to
  write and the contract blurs.
- **The loop catches at the network boundary.** Re-raise nothing — the process is meant to run
  forever.

## 4.6 Tests

```
tests/unit/ingestion/test_<backend>_mapping.py
tests/integration/test_<backend>_e2e.py
```

- The mapping test feeds fixture payloads and asserts the resulting `Signal` shape.
- The e2e test runs the collector against a live testbed and asserts the bus has new entries.

## 4.7 Smoke check

```bash
# 1. Start the collector
python -m inframind.ingestion.prometheus

# 2. Confirm signals are landing
redis-cli XLEN inframind.signals
# -> integer N (growing on every tick)

# 4. Peek at the stream
redis-cli XRANGE inframind.signals - + COUNT 1
```

## 4.8 Hard rules

- **Mapping first, bus second.** No raw backend objects cross `intake → persistent` boundary.
- **Idempotent publish.** Re-publishing on retry must not create duplicates the consumer has to
  clean up.
- **Catches at the network boundary, not inside `_to_signal`.** Mapping is pure; the loop is
  resilient.
- **No DL deps.** A collector that needs `pandas` or `numpy` is wrong.

## 4.9 Cross-reference

- **Skill:** `.claude/skills/add-collector/SKILL.md`.
- **Locked decisions:** D9 (redaction), D20 (bus is conduit, store is truth).
- **Upstream contract:** `docs/surface-map.md`.
- **Testbed:** `09-testbed-ops.md` in this set — the testbed the e2e test runs against.
