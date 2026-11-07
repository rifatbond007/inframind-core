"""The RCA worker: thin plumbing, but worth covering the bits `test_rank.py` does not.

Covers: `analyse()` returns a frozen `IncidentAnalysis` with the right `graph_version` and the
weighted ranker's primary candidate; `apply_graph_update` folds one `GraphUpdate` into the live
graph (the bus would deliver it; this exercises the *handler* path on the worker object);
`_symptom_services` prefers anomaly-bearing services and falls back to `incident.services` when
none are present; and `RCAWorker.__init__` wires the two `TypedStreamConsumer`s to the right
streams / models without ever touching Redis.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from inframind.common import stream_keys
from inframind.common.bus import TypedStreamConsumer
from inframind.common.graph_update import GraphUpdate
from inframind.common.incident import Incident
from inframind.common.signal import Severity
from inframind.rca.graph import ServiceGraph
from inframind.rca.worker import RCAWorker, _symptom_services

from .conftest import T0, make_anomaly


def _fault_anomalies() -> list:
    """Same canonical fault scenario `test_rank.py` uses: paymentservice is the root cause."""
    return [
        make_anomaly(
            "frontend", score=4.0, severity=Severity.CRITICAL, detector="error_spike", offset_s=0
        ),
        make_anomaly(
            "checkoutservice", score=2.0, severity=Severity.WARNING, detector="ewma", offset_s=10
        ),
        make_anomaly(
            "paymentservice",
            score=6.0,
            severity=Severity.CRITICAL,
            detector="error_spike",
            offset_s=5,
        ),
    ]


def _incident(
    anomalies: list, *, services: list[str] | None = None, open_state: bool = True
) -> Incident:
    """Build an `Incident` snapshot. `open=True` keeps `closed_at=None` so the ranker runs on
    `opened_at..updated_at`, the standard path for an open incident.
    """
    services_list = services or sorted({a.service for a in anomalies} | {"frontend"})
    return Incident(
        id="01ARZ3NDEKTSV4RRFFQ69G5FA1",
        revision=1,
        state="open" if open_state else "resolved",
        opened_at=T0,
        updated_at=T0 + timedelta(seconds=30),
        closed_at=None if open_state else T0 + timedelta(seconds=30),
        title="frontend errors spike",
        services=services_list,
        signals=[],
        anomalies=anomalies,
        severity=Severity.CRITICAL,
        fingerprint="abcdef0123456789",
    )


def test_analyse_returns_frozen_analysis_with_graph_version(warm_graph: ServiceGraph) -> None:
    """`analyse()` is the pure-RCA entry point: no I/O, returns an `IncidentAnalysis`."""
    worker = RCAWorker(MagicMock(), warm_graph)  # MagicMock: analyse() never touches Redis
    incident = _incident(_fault_anomalies())

    analysis = worker.analyse(incident)

    assert analysis.incident_id == incident.id
    assert analysis.graph_version == warm_graph.version()
    assert analysis.window == (incident.opened_at, incident.updated_at)
    # The weighted ranker picks the deepest + most severe candidate (test_rank locks this).
    assert analysis.primary is not None
    assert analysis.primary.service == "paymentservice"


async def test_apply_graph_update_folds_into_the_owned_graph(params) -> None:
    """`apply_graph_update` is the bus handler — it must fold into the worker-owned graph.

    Calling it twice keeps the edge below the walkability threshold (D21 contract: single
    writer); calling it `min_observations` times promotes it. Without this direct test the
    wiring still exercises the folding, but only via integration.
    """
    graph = ServiceGraph(params=params)
    worker = RCAWorker(MagicMock(), graph, params=params)

    await worker.apply_graph_update(
        GraphUpdate(caller="frontend", callee="cartservice", latency_ms=20.0, observed_at=T0)
    )
    await worker.apply_graph_update(
        GraphUpdate(caller="frontend", callee="cartservice", latency_ms=20.0, observed_at=T0)
    )
    assert not graph.is_walkable("frontend", "cartservice")

    await worker.apply_graph_update(
        GraphUpdate(caller="frontend", callee="cartservice", latency_ms=20.0, observed_at=T0)
    )
    assert graph.is_walkable("frontend", "cartservice")


def test_symptom_services_prefers_services_with_anomalies() -> None:
    """`_symptom_services` starts the walk from the services that actually carry anomalies."""
    anomalies = _fault_anomalies()
    incident = _incident(anomalies)

    symptoms = _symptom_services(incident)

    assert symptoms == sorted({a.service for a in anomalies})


def test_symptom_services_falls_back_to_incident_services() -> None:
    """An anomaly-less incident (Alertmanager-only open) still has a walkable starting set."""
    incident = _incident([], services=["frontend", "checkoutservice"])

    symptoms = _symptom_services(incident)

    assert symptoms == ["checkoutservice", "frontend"]


def test_worker_wires_two_typed_consumers_to_the_pinned_streams(warm_graph: ServiceGraph) -> None:
    """`RCAWorker.__init__` is the only place the consumer wiring lives — pin it.

    Both consumers are `TypedStreamConsumer` instances bound to the project's pinned stream
    keys (D21), with the right model type. No Redis call happens here; the assertion reads
    constructor attributes, which is what D21 actually requires.
    """
    redis = MagicMock()
    worker = RCAWorker(redis, warm_graph, graph_group="g", incident_group="i", consumer="c")

    graph_consumer = worker._graph_consumer
    incident_consumer = worker._incident_consumer

    assert isinstance(graph_consumer, TypedStreamConsumer)
    assert isinstance(incident_consumer, TypedStreamConsumer)
    assert graph_consumer._stream == stream_keys.GRAPH_UPDATES
    assert incident_consumer._stream == stream_keys.INCIDENTS
    assert graph_consumer._model is GraphUpdate
    assert incident_consumer._model is Incident
    assert graph_consumer._group == "g"
    assert incident_consumer._group == "i"


def test_worker_owns_change_events_passed_in_at_construction(warm_graph: ServiceGraph) -> None:
    """`change_events` is injected (the K8s watcher lands with P8); pin that contract."""
    from inframind.rca.change_correlation import ChangeEvent

    change = ChangeEvent(
        service="paymentservice",
        kind="deployment_rollout",
        resource="deploy/paymentservice",
        occurred_at=T0,
        revision="rev-1",
    )
    worker = RCAWorker(MagicMock(), warm_graph, change_events=[change])
    assert worker._change_events == [change]
