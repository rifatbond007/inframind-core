"""CallErrorDetector: failing client calls count against the callee (D30)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from inframind import metrics
from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.detection.call_error import CallErrorDetector, grpc_callee
from inframind.detection.config import ErrorSpikeParams

T0 = datetime(2026, 11, 16, 7, 31, tzinfo=UTC)


def span(
    at: float,
    *,
    kind: str = "client",
    operation: str = "hipstershop.CartService/GetCart",
    service: str = "frontend",
    n: int = 0,
) -> Signal:
    return Signal(
        ts=T0 + timedelta(seconds=at),
        source=Source.JAEGER,
        type=SignalType.TRACE,
        service=service,
        severity=Severity.CRITICAL,
        attrs={"span_id": f"{at}-{n}", "kind": kind, "operation": operation},
    )


def metric(service: str) -> Signal:
    return Signal(
        ts=T0 - timedelta(seconds=60),
        source=Source.PROMETHEUS,
        type=SignalType.METRIC,
        service=service,
        attrs={"metric": "cpu_cores", "value": 0.01, "labels": {"pod": f"{service}-x"}},
    )


def test_grpc_callee_reads_the_service_from_a_full_method() -> None:
    assert grpc_callee("hipstershop.CartService/GetCart") == "cartservice"
    assert grpc_callee("/hipstershop.ProductCatalogService/GetProduct") == "productcatalogservice"
    assert grpc_callee("CartService/GetCart") == "cartservice"
    assert grpc_callee("GET") is None
    assert grpc_callee(None) is None


def test_failing_calls_to_a_known_service_fire_against_the_callee() -> None:
    d = CallErrorDetector(ErrorSpikeParams(min_count=3))
    d.update(metric("cartservice"))
    found = [a for i in range(4) if (a := d.update(span(1 + i, n=i)))]
    assert found, "three failed GetCart calls did not fire"
    first = found[0]
    assert first.detector == "call_error"
    assert first.service == "cartservice"
    assert first.attrs["caller"] == "frontend"
    assert first.attrs["operation"] == "hipstershop.CartService/GetCart"


def test_an_unknown_callee_is_never_invented() -> None:
    """No signal ever came from `cartservice`: the operation name alone proves nothing."""
    d = CallErrorDetector(ErrorSpikeParams(min_count=3))
    assert [d.update(span(1 + i, n=i)) for i in range(6)] == [None] * 6


def test_server_spans_and_self_calls_are_not_attributed() -> None:
    d = CallErrorDetector(ErrorSpikeParams(min_count=3))
    d.update(metric("cartservice"))
    server = [d.update(span(1 + i, kind="server", n=i)) for i in range(6)]
    own = [
        d.update(span(10 + i, service="cartservice", n=i)) for i in range(6)
    ]  # cartservice calling CartService is not a downstream failure
    assert server == [None] * 6
    assert own == [None] * 6


def _call_errors(caller: str, callee: str) -> float:
    return metrics.CALL_ERRORS.labels(caller=caller, callee=callee)._value.get()


def test_every_attributed_failing_call_is_counted_for_the_service_graph() -> None:
    """Each failing call counts once, before any spike fires (Grafana draws the edge red)."""
    d = CallErrorDetector(ErrorSpikeParams(min_count=3))
    d.update(metric("cartservice"))
    before = _call_errors("frontend", "cartservice")
    for i in range(2):  # below min_count: no anomaly yet, but both calls are real failures
        assert d.update(span(1 + i, n=i)) is None
    assert _call_errors("frontend", "cartservice") == before + 2


def test_unattributed_spans_are_not_counted() -> None:
    """An unknown callee, a server span or a self-call never becomes a graph edge."""
    d = CallErrorDetector(ErrorSpikeParams(min_count=3))
    before = _call_errors("frontend", "cartservice")
    d.update(span(1))  # cartservice never seen: unknown callee
    d.update(metric("cartservice"))
    d.update(span(2, kind="server"))
    assert _call_errors("frontend", "cartservice") == before
