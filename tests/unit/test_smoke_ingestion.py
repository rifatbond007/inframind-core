"""The pure evaluation half of testbed/smoke_ingestion.py (the P3 gate); no cluster needed."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from inframind.common.graph_update import GraphUpdate
from inframind.common.signal import Signal, SignalType, Source

SCRIPT = Path(__file__).resolve().parents[2] / "testbed" / "smoke_ingestion.py"
spec = importlib.util.spec_from_file_location("smoke_ingestion", SCRIPT)
assert spec and spec.loader
smoke = importlib.util.module_from_spec(spec)
sys.modules["smoke_ingestion"] = smoke
spec.loader.exec_module(smoke)

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
RUN = "a1b2c3"
SERVICES = [
    "frontend",
    "cartservice",
    "adservice",
    "paymentservice",
    "currencyservice",
    "emailservice",
]
EDGES = [
    ("frontend", "productcatalogservice"),
    ("frontend", "recommendationservice"),
    ("recommendationservice", "productcatalogservice"),
]


def sig(source: Source, kind: SignalType, service: str, **attrs: Any) -> Signal:
    trace_id = attrs.pop("trace_id", None)
    return Signal(ts=NOW, source=source, type=kind, service=service, trace_id=trace_id, attrs=attrs)


def good_signals() -> list[Signal]:
    out = [sig(Source.PROMETHEUS, SignalType.METRIC, s, metric="cpu_cores") for s in SERVICES]
    out.append(
        sig(
            Source.LOKI,
            SignalType.LOG,
            "frontend",
            level="error",
            grpc_code="NotFound",
            message=f"request error: no product with ID NOPE-{RUN}",
        )
    )
    out.append(
        sig(
            Source.JAEGER,
            SignalType.TRACE,
            "frontend",
            trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            span_id="b",
            status_description=f"no product with ID NOPE-{RUN}",
        )
    )
    out.append(
        sig(
            Source.ALERTMANAGER,
            SignalType.ALERT,
            "frontend",
            alertname="InframindSmoke",
            labels={"smoke_run": RUN},
        )
    )
    return out


def good_updates() -> list[GraphUpdate]:
    return [GraphUpdate(caller=a, callee=b, latency_ms=1.0, observed_at=NOW) for a, b in EDGES]


def by_name(results: list[Any]) -> dict[str, Any]:
    return {r.name: r for r in results}


def test_a_healthy_run_passes_every_check() -> None:
    results = smoke.evaluate(good_signals(), good_updates(), RUN)
    assert [r.name for r in results] == [
        "prometheus",
        "loki",
        "jaeger-edges",
        "jaeger-error",
        "alertmanager",
    ]
    assert all(r.ok for r in results), [r for r in results if not r.ok]


def test_edges_present_returns_caller_callee_pairs() -> None:
    assert smoke.edges_present(good_updates()) == set(EDGES)


def test_distinct_services_filters_by_source() -> None:
    assert smoke.distinct_services(good_signals(), Source.PROMETHEUS) == set(SERVICES)
    assert smoke.distinct_services(good_signals(), Source.LOKI) == {"frontend"}


def test_a_missing_edge_fails_and_is_named() -> None:
    updates = [u for u in good_updates() if (u.caller, u.callee) != EDGES[2]]
    r = by_name(smoke.evaluate(good_signals(), updates, RUN))["jaeger-edges"]
    assert not r.ok
    assert "recommendationservice->productcatalogservice" in r.detail


def test_too_few_prometheus_services_fails_with_the_count() -> None:
    signals = [
        s
        for s in good_signals()
        if not (s.source is Source.PROMETHEUS and s.service in SERVICES[:2])
    ]
    r = by_name(smoke.evaluate(signals, good_updates(), RUN))["prometheus"]
    assert not r.ok
    assert "4" in r.detail


@pytest.mark.parametrize(
    ("check", "drop"),
    [
        ("loki", lambda s: s.source is Source.LOKI),
        ("jaeger-error", lambda s: s.source is Source.JAEGER),
        ("alertmanager", lambda s: s.source is Source.ALERTMANAGER),
    ],
)
def test_a_missing_source_fails_its_check_only(check: str, drop: Any) -> None:
    signals = [s for s in good_signals() if not drop(s)]
    results = by_name(smoke.evaluate(signals, good_updates(), RUN))
    assert not results[check].ok
    assert [n for n, r in results.items() if not r.ok] == [check]


def test_signals_from_another_run_do_not_satisfy_the_checks() -> None:
    results = smoke.evaluate(good_signals(), good_updates(), "ffffff")
    failed = {r.name for r in results if not r.ok}
    assert failed == {"loki", "jaeger-error", "alertmanager"}


def test_loki_error_without_a_grpc_code_fails() -> None:
    signals = [s for s in good_signals() if s.source is not Source.LOKI]
    signals.append(
        sig(Source.LOKI, SignalType.LOG, "frontend", level="error", message=f"NOPE-{RUN}")
    )
    assert not by_name(smoke.evaluate(signals, good_updates(), RUN))["loki"].ok


def test_a_jaeger_error_without_a_w3c_trace_id_fails() -> None:
    signals = [s for s in good_signals() if s.source is not Source.JAEGER]
    signals.append(
        sig(
            Source.JAEGER,
            SignalType.TRACE,
            "frontend",
            span_id="b",
            status_description=f"NOPE-{RUN}",
        )
    )
    assert not by_name(smoke.evaluate(signals, good_updates(), RUN))["jaeger-error"].ok
