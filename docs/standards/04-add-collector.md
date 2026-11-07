# 4. Add a collector — Prometheus / Loki / Jaeger / Alertmanager
> standards · Python conventions · InfraMind. Pairs with the `add-collector` skill in `.claude/skills/add-collector/SKILL.md`. This file is the rationale and the worked example. The three pull collectors live in `src/inframind/ingestion/`; the Alertmanager webhook is the only push ingress and lives in `src/inframind/api/alertmanager.py` (section 4.10).

## 4.1 The shape every pull collector has

```
src/inframind/ingestion/<backend>.py
```

```python
from __future__ import annotations

class <Backend>Collector:
    """Polls <backend> on an interval and publishes new observations to the bus."""

    def __init__(
        self,
        client: httpx.AsyncClient,      # base_url is the backend, see runner.py
        redis: Redis,
        *,
        interval_s: int = 30,
        state: State | None = None,     # CollectorState(redis, "<backend>") survives restarts
        clock: Callable[[], datetime] = utcnow,
    ) -> None: ...

    async def poll_once(self) -> int: ...        # one pass; returns the number published
    async def run(self, stop: asyncio.Event) -> None: ...   # poll until `stop` is set


def <row|entry|trace>_to_signal(...) -> Signal | None: ...   # pure mapping, module level


async def amain() -> None:
    await run_collector(
        lambda client, redis, interval_s: <Backend>Collector(client, redis, interval_s=interval_s),
        url_env="<BACKEND>_URL", default_url="http://localhost:3xxxx",
        interval_env="<BACKEND>_INTERVAL_S",
    )
```

`ingestion/runner.py` (`run_collector`) builds the clients from the environment (`<BACKEND>_URL`,
`<BACKEND>_INTERVAL_S`, `REDIS_URL`), installs the SIGINT/SIGTERM handlers and closes both clients
on every exit path. A collector module must not repeat that code.

## 4.2 Three responsibilities, three places

- **The mapping function** (`row_to_signal`, `entry_to_signal`, `trace_to_outputs`) is pure: it
  takes the backend's shape and returns `Signal`(s), or `None` for an observation that is not a
  signal. It never touches the network, so its tests need no mocks.
- **`poll_once()`** queries, maps and publishes one pass. It catches at the network boundary
  (`httpx.HTTPError`, bad JSON, a non-`success` status), logs with context, and returns the
  count. One failing query must not block the others.
- **`run(stop)`** loops `poll_once()` until `stop` is set and never raises on backend or bus
  failures.

A collector imports only from `common/` (and its own package). It never calls `detection`.

## 4.3 Publishing and not republishing

Publish with `inframind.common.bus.publish(redis, signal, stream=…)`; it XADDs one field named
`data` with an approximate `MAXLEN`, returns `None` for a signal whose service is `unknown`, and
raises `BusError` when Redis rejects the write. The Jaeger collector also uses
`publish_graph_update` for `stream:graph-updates` (D21). Never call `xadd` directly.

The bus consumer deduplicates on `Signal.id`, but a pull collector re-reads an overlap behind its
cursor to absorb backend delay, and a fresh `Signal` gets a fresh id. So the collector must itself
avoid republishing: keep a cursor and a set of already-published keys in a `State`
(`common/collector_state.py`). **Mark a key only after its publish succeeded**, and hold the
cursor back when the bus failed, so a Redis outage retries instead of losing data. Give the
production collector `CollectorState(redis, "<backend>")` so a restart does not replay.

## 4.4 Worked example: the Prometheus collector

Online Boutique's services are gRPC and expose no per-service request metrics, so the queries in
`ingestion/prometheus.py` are per-pod cAdvisor and kube-state-metrics series:

```python
DEFAULT_QUERIES = (
    MetricQuery("cpu_cores", 'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{…}[1m]))'),
    MetricQuery("memory_working_set_bytes", 'sum by (namespace, pod) (container_memory_working_set_bytes{…})'),
    MetricQuery("container_restarts_total", 'sum by (namespace, pod) (kube_pod_container_status_restarts_total{…})'),
)
```

`row_to_signal` maps the latest sample of each series (see standard 03 §3.6) and
`PrometheusCollector.poll_once` publishes it, skipping any sample not newer than the last one
published for that series. Request errors and latency come from Loki and Jaeger instead.

## 4.5 The collector discipline (and why)

- **One collector per backend.** Multiple collectors for the same backend are merged into one
  with an internal list of queries or services, because retries and back-off are easier to
  coordinate when one loop owns them.
- **Pull, not push** — for Prometheus, Loki and Jaeger. The Alertmanager webhook is the only push
  ingress (Alertmanager has no pull API worth using).
- **The mapping is pure.** If it touches the network its test is harder to write and the
  contract blurs.
- **The loop catches at the network boundary.** The process is meant to run forever.
- **Redact before the bus.** Free text (log messages, span status descriptions, alert
  annotations) goes through `common.redact` and is truncated *after* redaction, so a cut token
  cannot leak a fragment (D9). Store a truncated message, never the raw payload.
- **Quiet backends are normal.** Kubelet probes keep producing traces and logs; a collector test
  must never assert "a second poll publishes nothing" against a live backend, only "nothing is
  published twice".

## 4.6 Tests

```
tests/unit/ingestion/test_<backend>.py        # mapping + the poll loop
tests/integration/test_<backend>_e2e.py       # live backend; skips when unreachable
```

- The unit test feeds fixture payloads to the mapping function and drives `poll_once` through
  `httpx.MockTransport` with `fakeredis`. Use realistic fixtures: pod suffixes come from
  Kubernetes' alphabet (`jfpxt`, not `abcde`).
- The integration test runs the collector against the live testbed into throwaway streams
  (`stream:test-<uuid>`) and deletes them afterwards.
- `make smoke-ingestion` is the P3 gate across all four paths (see `testbed/README.md`).

## 4.7 Smoke check

```bash
# 1. Run a collector (the venv must have the package installed: `make install`)
PROM_INTERVAL_S=10 python -m inframind.ingestion.prometheus     # Ctrl+C stops it cleanly

# 2. Confirm signals are landing (the host has no redis-cli; use the compose container)
docker exec inframind-redis redis-cli XLEN stream:signals
docker exec inframind-redis redis-cli XREVRANGE stream:signals + - COUNT 1
```

| Collector | Module | Default URL (testbed NodePort) | Env keys |
|---|---|---|---|
| Prometheus | `inframind.ingestion.prometheus` | `http://localhost:30090` | `PROMETHEUS_URL`, `PROM_INTERVAL_S` |
| Loki | `inframind.ingestion.loki` | `http://localhost:30100` | `LOKI_URL`, `LOKI_INTERVAL_S` |
| Jaeger | `inframind.ingestion.jaeger` | `http://localhost:30686` | `JAEGER_URL`, `JAEGER_INTERVAL_S` |

## 4.8 Hard rules

- **Mapping first, bus second.** No raw backend objects cross the ingestion boundary.
- **Never publish twice, never lose one.** Cursor plus seen-set, marked after a successful
  publish.
- **Catch at the network boundary, not inside the mapping function.**
- **No DL deps.** A collector that needs `pandas` or `numpy` is wrong.
- **No secrets or PII on the bus.** Redact free text before it leaves the collector.

## 4.9 Cross-reference

- **Skill:** `.claude/skills/add-collector/SKILL.md`.
- **Locked decisions:** D9 (redaction), D20 (bus is conduit, store is truth), D21 (graph updates).
- **Contract:** `03-signal-schema.md` and `13-service-graph-contract.md`.
- **Testbed:** `09-testbed-ops.md` — the testbed the live tests and `make smoke-ingestion` run against.

## 4.10 The Alertmanager webhook (the push ingress)

FastAPI may only be imported by `api/` (standards 01 §1.3), so the route and its pure mapper
live together in `src/inframind/api/alertmanager.py`: `POST /webhooks/alertmanager` calls
`alerts_to_signals(payload, request_id=…, ingested_at=…)` and publishes with `bus.publish`.
Mapping rules and the reason `service` is resolved from `pod` before the `service` label are in
standard 03 §3.7. The route authenticates first (`Authorization: Bearer <secret>` against
`INFRAMIND_ALERT_WEBHOOK_SECRET`, constant-time compare), then bounds the input (1 MiB body,
1000 alerts), then maps; a bus failure returns 503 so Alertmanager retries. Run it with
`python -m inframind.api.app`; the in-cluster receiver is configured in
`testbed/helm-values/kube-prometheus-stack.yaml`.
