"""The walk: depth truncation, cycle safety, and the callee direction (standard 06.8).

Covers D2 explicitly — the walk starts at the symptom and moves **toward callees**, and it
never walks toward callers (predecessors).
"""

from __future__ import annotations

from datetime import timedelta

from inframind.common.graph_update import GraphUpdate
from inframind.rca.config import RCAParams
from inframind.rca.graph import ServiceGraph
from inframind.rca.traversal import walk_toward_callees

from .conftest import BOUTIQUE_EDGES, T0


def _warm(graph: ServiceGraph, edges: list[tuple[str, str]], params: RCAParams) -> ServiceGraph:
    for caller, callee in edges:
        for i in range(params.min_observations):
            graph.apply_update(
                GraphUpdate(
                    caller=caller,
                    callee=callee,
                    latency_ms=10.0,
                    observed_at=T0 + timedelta(seconds=i),
                )
            )
    return graph


def test_walk_returns_the_symptom_at_depth_zero(warm_graph: ServiceGraph) -> None:
    walked = walk_toward_callees(warm_graph, "frontend")
    services = [c.service for c in walked]

    assert "frontend" in services
    assert walked[0].service == "frontend"
    assert walked[0].depth == 0


def test_walk_moves_toward_callees_not_callers(warm_graph: ServiceGraph) -> None:
    """D2: from `paymentservice` (a callee) the walk must NOT reach its caller `frontend`."""
    walked = walk_toward_callees(warm_graph, "paymentservice")
    services = {c.service for c in walked}

    # paymentservice is a leaf, so only itself is reachable.
    assert services == {"paymentservice"}
    assert "frontend" not in services
    assert "checkoutservice" not in services


def test_walk_from_frontend_finds_deep_callees(warm_graph: ServiceGraph) -> None:
    walked = walk_toward_callees(warm_graph, "frontend")
    by_service = {c.service: c.depth for c in walked}

    assert by_service["cartservice"] == 1
    assert by_service["checkoutservice"] == 1
    assert by_service["paymentservice"] == 2


def test_max_depth_truncates_the_walk(warm_graph: ServiceGraph) -> None:
    """`max_depth=1` stops the walk before reaching the depth-2 services."""
    walked = walk_toward_callees(warm_graph, "frontend", max_depth=1)
    services = {c.service for c in walked}

    assert "checkoutservice" in services
    assert "paymentservice" not in services
    assert all(c.depth <= 1 for c in walked)


def test_walk_terminates_on_a_cycle(params: RCAParams) -> None:
    """A cyclic topology does not loop forever; every node appears once."""
    graph = ServiceGraph(params=params)
    _warm(graph, [("a", "b"), ("b", "c"), ("c", "a")], params)

    walked = walk_toward_callees(graph, "a", max_depth=10)
    services = [c.service for c in walked]

    assert set(services) == {"a", "b", "c"}
    assert len(services) == len(set(services))


def test_walk_skips_edges_below_min_observations(params: RCAParams) -> None:
    """An edge observed fewer than `min_observations` times is invisible to the walk."""
    graph = ServiceGraph(params=params)
    graph.apply_update(
        GraphUpdate(caller="frontend", callee="cartservice", latency_ms=10.0, observed_at=T0)
    )
    graph.apply_update(
        GraphUpdate(caller="cartservice", callee="paymentservice", latency_ms=10.0, observed_at=T0)
    )

    walked = walk_toward_callees(graph, "frontend")
    services = {c.service for c in walked}

    # Neither edge met the threshold, so only the symptom itself is reachable.
    assert services == {"frontend"}


def test_walk_is_ordered_by_depth_then_service(warm_graph: ServiceGraph) -> None:
    walked = walk_toward_callees(warm_graph, "frontend")
    keys = [(c.depth, c.service) for c in walked]
    assert keys == sorted(keys)


def test_walk_path_records_the_route(warm_graph: ServiceGraph) -> None:
    walked = {c.service: c for c in walk_toward_callees(warm_graph, "frontend")}
    assert walked["paymentservice"].path == ("frontend", "checkoutservice", "paymentservice")


def test_walk_from_a_service_with_no_outgoing_edges_is_just_itself(
    params: RCAParams,
) -> None:
    graph = ServiceGraph(params=params)
    graph.seed_from_signals([("frontend", "cartservice")])
    walked = walk_toward_callees(graph, "cartservice")
    assert [c.service for c in walked] == ["cartservice"]


def test_default_max_depth_is_pinned_by_the_standard(warm_graph: ServiceGraph) -> None:
    """The default `max_depth` is 4 (standard 06.3), not a hard-coded different value."""
    assert RCAParams().max_depth == 4
    # With default depth 4, a depth-2 chain from frontend is fully reachable.
    walked = walk_toward_callees(warm_graph, "frontend")
    assert any(c.service == "paymentservice" for c in walked)


def test_all_boutique_edges_are_walkable(warm_graph: ServiceGraph) -> None:
    """Sanity: the warm fixture has every real edge walkable."""
    for caller, callee in BOUTIQUE_EDGES:
        assert warm_graph.is_walkable(caller, callee)
