"""What the D7 baselines see: evidence from the replayed incident, never a guess."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from inframind.common.anomaly import Anomaly
from inframind.common.graph_update import GraphUpdate
from inframind.common.incident import Incident
from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.evaluation.baselines import (
    DeepestErrorSpanBaseline,
    HighestErrorServiceBaseline,
    RandomServiceBaseline,
    RawAlertmanagerBaseline,
)
from inframind.evaluation.runner.main import (
    _baseline_input_from_trace,
    _callee_of,
    _run_baselines,
)
from inframind.evaluation.runner.replay import ReplayTrace
from inframind.evaluation.scenarios import load_set
from inframind.rca.graph import ServiceGraph

T0 = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
APP = frozenset({"frontend", "cartservice", "checkoutservice", "productcatalogservice"})


def _sig(service: str, at: float, source: Source, **attrs: object) -> Signal:
    kind = {Source.JAEGER: SignalType.TRACE, Source.LOKI: SignalType.LOG}.get(
        source, SignalType.METRIC if source is Source.PROMETHEUS else SignalType.ALERT
    )
    return Signal(
        ts=T0 + timedelta(seconds=at),
        source=source,
        type=kind,
        service=service,
        severity=Severity.CRITICAL if source is not Source.PROMETHEUS else Severity.INFO,
        attrs=dict(attrs),
    )


def _graph() -> ServiceGraph:
    """frontend -> productcatalogservice, frontend -> checkoutservice (no cart: no traces)."""
    graph = ServiceGraph(seed_path=None)
    for callee in ("productcatalogservice", "checkoutservice"):
        for _ in range(5):
            graph.apply_update(
                GraphUpdate(caller="frontend", callee=callee, latency_ms=1.0, observed_at=T0)
            )
    return graph


def _incident() -> Incident:
    anomaly = Anomaly(
        ts=T0 + timedelta(seconds=1),
        detector="error_spike",
        service="frontend",
        signal_id="s",
        score=5.0,
        severity=Severity.CRITICAL,
    )
    return Incident(
        revision=1,
        state="open",
        opened_at=T0,
        updated_at=T0 + timedelta(seconds=60),
        title="frontend: error_spike anomaly",
        services=["frontend"],
        anomalies=[anomaly],
        severity=Severity.CRITICAL,
        fingerprint="0123abcd",
    )


def _trace(*, with_incident: bool = True) -> ReplayTrace:
    """The dev-01 shape: frontend's GetCart client spans fail; cartservice has no spans."""
    signals = [_sig(s, -300, Source.PROMETHEUS, metric="cpu_cores", value=0.01) for s in APP]
    signals += [
        _sig(
            "frontend",
            2 + i,
            Source.JAEGER,
            span_id=f"c{i}",
            kind="client",
            operation="hipstershop.CartService/GetCart",
        )
        for i in range(4)
    ]
    signals += [
        _sig("frontend", 2 + i, Source.JAEGER, span_id=f"s{i}", kind="server", operation="GET")
        for i in range(4)
    ]
    signals += [
        _sig("frontend", 3 + i, Source.LOKI, message="could not retrieve cart") for i in range(6)
    ]
    signals += [_sig("checkoutservice", 5, Source.LOKI, message="cart unavailable")]
    signals += [_sig("frontend", 900, Source.LOKI, message="later, outside the window")] * 1
    signals.sort(key=lambda s: (s.ts, s.id))
    return ReplayTrace(
        signals=signals,
        graph=_graph(),
        onset_ts=T0,
        incidents=[_incident()] if with_incident else [],
    )


def test_a_client_span_names_its_callee() -> None:
    assert _callee_of("hipstershop.CartService/GetCart", APP) == "cartservice"
    assert _callee_of("hipstershop.ProductCatalogService/GetProduct", APP) == (
        "productcatalogservice"
    )
    assert _callee_of("GET", APP) is None
    assert _callee_of("hipstershop.AdService/GetAds", APP) is None  # not a known service


def test_evidence_is_counted_in_the_incident_window_only() -> None:
    baseline_input = _baseline_input_from_trace(_trace())
    # 8 spans + 6 log lines in the window; the line at +900 s is outside it.
    assert baseline_input.error_events_by_service == {"frontend": 14, "checkoutservice": 1}


def test_a_failing_client_span_puts_its_callee_deepest() -> None:
    """frontend is a root (depth 0); its failing GetCart span points one hop down."""
    baseline_input = _baseline_input_from_trace(_trace())
    assert baseline_input.error_depth_by_service == {"cartservice": 1, "frontend": 0}
    assert DeepestErrorSpanBaseline().rank(baseline_input) == ["cartservice", "frontend"]


def test_highest_error_service_ranks_only_services_with_errors() -> None:
    ranked = HighestErrorServiceBaseline().rank(_baseline_input_from_trace(_trace()))
    assert ranked == ["frontend", "checkoutservice"]  # no alphabetical tail of guesses


def test_raw_alertmanager_without_an_alert_ranks_nothing() -> None:
    assert RawAlertmanagerBaseline().rank(_baseline_input_from_trace(_trace())) == []


def test_random_draws_a_fresh_order_per_run_seed() -> None:
    baseline_input = _baseline_input_from_trace(_trace())
    orders = {tuple(RandomServiceBaseline(seed=s).rank(baseline_input)) for s in range(20)}
    assert len(orders) > 1


def test_pagerank_runs_on_the_incident_and_nothing_without_one() -> None:
    spec = load_set("dev")[0]
    trace = _trace()
    rows = _run_baselines(
        spec=spec, seed=1, repeat=0, baseline_input=_baseline_input_from_trace(trace), trace=trace
    )
    pagerank = next(r for r in rows if r.method == "pagerank")
    assert pagerank.detected and pagerank.predicted_root_cause

    quiet = _trace(with_incident=False)
    rows = _run_baselines(
        spec=spec, seed=1, repeat=0, baseline_input=_baseline_input_from_trace(quiet), trace=quiet
    )
    assert next(r for r in rows if r.method == "pagerank").detected is False


def _noise_incident(opened_s: float, closed_s: float | None) -> Incident:
    inc = _incident()
    return inc.model_copy(
        update={
            "id": f"noise-{opened_s}",
            "opened_at": T0 + timedelta(seconds=opened_s),
            "updated_at": T0 + timedelta(seconds=closed_s if closed_s is not None else 30),
            "closed_at": T0 + timedelta(seconds=closed_s) if closed_s is not None else None,
            "state": "resolved" if closed_s is not None else "open",
        }
    )


def test_a_noise_incident_resolved_before_the_fault_is_not_a_detection() -> None:
    """dev-02's no-fault repeats: one frontend blip at -561 s resolved long before onset."""
    from inframind.evaluation.runner.replay import baseline_problem

    noise = _noise_incident(-561, -400)
    trace = ReplayTrace(
        signals=_trace().signals,
        graph=_graph(),
        onset_ts=T0,
        incidents=[_incident()],
        noise_incidents=[noise],
    )
    assert baseline_problem(trace) is None
    assert trace.first_open == T0  # the noise did not become a 0 s "detection"


def test_an_incident_open_when_the_fault_starts_makes_the_baseline_dirty() -> None:
    from inframind.evaluation.runner.replay import baseline_problem

    still_open = _noise_incident(-120, None)
    trace = ReplayTrace(
        signals=_trace().signals, graph=_graph(), onset_ts=T0, incidents=[still_open]
    )
    assert baseline_problem(trace) == "an incident was already open when the fault started"


def test_an_incident_sized_burst_before_the_fault_makes_the_baseline_dirty() -> None:
    from inframind.evaluation.runner.replay import PRE_FAULT_ANOMALY_LIMIT, baseline_problem

    burst = [
        _sig("frontend", -300 + i * 0.1, Source.LOKI, message="earlier fault") for i in range(60)
    ]
    signals = sorted(_trace().signals + burst, key=lambda s: (s.ts, s.id))
    trace = ReplayTrace(signals=signals, graph=_graph(), onset_ts=T0, incidents=[_incident()])
    problem = baseline_problem(trace)
    assert problem is not None and f"limit {PRE_FAULT_ANOMALY_LIMIT}" in problem
