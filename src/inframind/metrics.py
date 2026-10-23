"""The six InfraMind Prometheus metrics (standard 11 section 11.6) plus a text-exposition helper.

Every InfraMind process ships these. The api process appends `render_extra()` to its hand-rolled
`/metrics`; every worker process serves the same registry over its own `/metrics` on port 9100
via `start_http_server`. The chart's `ServiceMonitor` scrapes the api; the `PodMonitor` scrapes
each worker pod.

Metric names are pinned. The paper's observability section and the chart's monitor templates
both hard-code them:

| Metric | Type | Labels | Increment site |
|---|---|---|---|
| `inframind_signals_received_total` | Counter | `source`, `type` | `common.bus.publish` |
| `inframind_anomalies_emitted_total` | Counter | `detector` | `detection.engine.process` |
| `inframind_incidents_opened_total` | Counter | `service` | `correlation.tracker._open` |
| `inframind_rca_ranking_duration_seconds` | Histogram | none | `rca.rank.rank_candidates` |
| `inframind_llm_call_duration_seconds` | Histogram | none | `llm.pipeline.explain` |
| `inframind_alerts_sent_total` | Counter | `channel`, `severity` | `alerting.router.dispatch` |

Instrumentation is deliberately thin: each increment is one `.labels(...).inc()` at the site
that already knows the value. Nothing here queries Prometheus, nothing here stores state, and
nothing here may raise into a caller's control flow. A metrics failure must never take down a
collector, so every `record_*` / `observe_*` helper suppresses exceptions and degrades to a
no-op.
"""

from __future__ import annotations

import contextlib
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

#: Buckets for the two duration histograms. Tuned to the pipeline's actual latencies: sub-10 ms
#: is the graph-only ranking path, seconds is an LLM call. Prometheus's default buckets stop at
#: 10 s and are too coarse in the 10-100 ms band where ranking actually lives.
DURATION_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

#: The port every worker process serves `/metrics` on (standard 11 section 11.12). The api process
#: uses 8080 and is scraped by the ServiceMonitor instead.
WORKER_METRICS_PORT = 9100

#: A private registry, not the global default. Two reasons: the api process has its own
#: hand-rolled exposition and we do not want `generate_latest()` on the default registry dragging
#: in unrelated default collectors; and a private registry makes `render_extra()` deterministic,
#: which `tests/unit/api/test_metrics.py` asserts on.
REGISTRY = CollectorRegistry()

SIGNALS_RECEIVED = Counter(
    "inframind_signals_received",
    "Signals successfully published to the Redis bus.",
    ["source", "type"],
    registry=REGISTRY,
)

ANOMALIES_EMITTED = Counter(
    "inframind_anomalies_emitted",
    "Anomalies published by a detector.",
    ["detector"],
    registry=REGISTRY,
)

INCIDENTS_OPENED = Counter(
    "inframind_incidents_opened",
    "Incidents opened by the correlation tracker.",
    ["service"],
    registry=REGISTRY,
)

RCA_RANKING_DURATION = Histogram(
    "inframind_rca_ranking_duration_seconds",
    "Wall-clock time spent ranking RCA candidates.",
    buckets=DURATION_BUCKETS,
    registry=REGISTRY,
)

LLM_CALL_DURATION = Histogram(
    "inframind_llm_call_duration_seconds",
    "Wall-clock time spent in an LLM explainer call, cache hits included at zero.",
    buckets=DURATION_BUCKETS,
    registry=REGISTRY,
)

ALERTS_SENT = Counter(
    "inframind_alerts_sent",
    "Alert dispatches by sink and severity.",
    ["channel", "severity"],
    registry=REGISTRY,
)

#: Every metric this module owns, for the test that asserts the set is exactly these six.
ALL_METRICS: tuple[Any, ...] = (
    SIGNALS_RECEIVED,
    ANOMALIES_EMITTED,
    INCIDENTS_OPENED,
    RCA_RANKING_DURATION,
    LLM_CALL_DURATION,
    ALERTS_SENT,
)

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def render_extra() -> str:
    """Return the six InfraMind metrics in Prometheus text exposition format.

    The api process appends this to its hand-rolled `/metrics` body, which already carries
    `inframind_up` and `inframind_uptime_seconds`. Workers call `render_extra()` indirectly
    through `prometheus_client.start_http_server`, which owns its own exposition; this function
    is the api's seam.

    With no samples recorded, `prometheus_client` still emits the `# HELP` / `# TYPE` lines for
    each family, so the six names are always present. That is deliberate: a scrape finding a
    metric *missing* should mean "the family was never registered", which is a bug, rather than
    "nothing has happened yet".

    Returns:
        A deterministic text block. Never raises.
    """
    with contextlib.suppress(Exception):
        return generate_latest(REGISTRY).decode("utf-8")
    return ""


def content_type() -> str:
    """The exposition content type the workers' HTTP server advertises."""
    return _CONTENT_TYPE


def record_signal(source: str, signal_type: str) -> None:
    """Count one signal published to the bus. Never raises."""
    with contextlib.suppress(Exception):
        SIGNALS_RECEIVED.labels(source=source, type=signal_type).inc()


def record_anomaly(detector: str) -> None:
    """Count one anomaly published by a detector. Never raises."""
    with contextlib.suppress(Exception):
        ANOMALIES_EMITTED.labels(detector=detector).inc()


def record_incident_opened(service: str) -> None:
    """Count one incident opened for `service`. Never raises."""
    with contextlib.suppress(Exception):
        INCIDENTS_OPENED.labels(service=service).inc()


def record_alert(channel: str, severity: str) -> None:
    """Count one alert dispatched on `channel` at `severity`. Never raises."""
    with contextlib.suppress(Exception):
        ALERTS_SENT.labels(channel=channel, severity=severity).inc()


def observe_ranking(duration_s: float) -> None:
    """Record a ranking duration in seconds. Never raises."""
    with contextlib.suppress(Exception):
        RCA_RANKING_DURATION.observe(duration_s)


def observe_llm_call(duration_s: float) -> None:
    """Record an LLM call duration in seconds. Never raises."""
    with contextlib.suppress(Exception):
        LLM_CALL_DURATION.observe(duration_s)


def start_http_server(port: int = WORKER_METRICS_PORT, addr: str = "0.0.0.0") -> Any:
    """Serve `REGISTRY` over HTTP on `port` in this process's background thread.

    Called from each worker's `amain` after logging is configured. `prometheus_client` starts a
    daemon thread; the process's own event loop is untouched, so a worker needs no extra
    dependency just to expose metrics.

    Binds `0.0.0.0` because the PodMonitor scrapes the pod IP, not localhost.

    A bind failure is logged and swallowed: a worker that cannot expose metrics is still a
    worker that processes signals. Losing observability is not a reason to lose the pipeline.

    Args:
        port: TCP port to listen on. Standard 11 section 11.12 pins 9100.
        addr: Bind address.

    Returns:
        Whatever `prometheus_client.start_http_server` returns (the server and thread), or
        `None` if the bind failed.
    """
    from prometheus_client import start_http_server as _start

    from inframind.common.logging import get_logger

    try:
        return _start(port, addr=addr, registry=REGISTRY)
    except OSError as e:
        get_logger(__name__).warning("metrics_http_server_bind_failed", port=port, error=str(e))
        return None


__all__ = [
    "ALERTS_SENT",
    "ALL_METRICS",
    "ANOMALIES_EMITTED",
    "DURATION_BUCKETS",
    "INCIDENTS_OPENED",
    "LLM_CALL_DURATION",
    "RCA_RANKING_DURATION",
    "REGISTRY",
    "SIGNALS_RECEIVED",
    "WORKER_METRICS_PORT",
    "content_type",
    "observe_llm_call",
    "observe_ranking",
    "record_alert",
    "record_anomaly",
    "record_incident_opened",
    "record_signal",
    "render_extra",
    "start_http_server",
]
