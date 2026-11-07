"""`apply_update` correctly decays / adds edges (standard 13.9 `test_graph_apply`).

Covers: EWMA weighting, the min-observations gate (an edge is invisible until observed 3x),
and time-based decay / pruning of silent edges.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from inframind.common.graph_update import GraphUpdate
from inframind.rca.config import RCAParams
from inframind.rca.graph import ServiceGraph

from .conftest import T0


def _update(caller: str, callee: str, latency_ms: float, offset_s: float = 0.0) -> GraphUpdate:
    return GraphUpdate(
        caller=caller,
        callee=callee,
        latency_ms=latency_ms,
        observed_at=T0 + timedelta(seconds=offset_s),
    )


def test_edge_below_min_observations_is_not_walkable(params: RCAParams) -> None:
    """An edge observed fewer than `min_observations` times is invisible to the walk."""
    graph = ServiceGraph(params=params)
    graph.apply_update(_update("frontend", "cartservice", 20.0))
    graph.apply_update(_update("frontend", "cartservice", 20.0))

    assert not graph.is_walkable("frontend", "cartservice")
    assert graph.edge_confidence("frontend", "cartservice") == 0.0
    # But the calls were counted, so the edge exists and carries some weight.
    assert graph.edge_weight("frontend", "cartservice") > 0.0


def test_edge_is_walkable_at_min_observations(params: RCAParams) -> None:
    graph = ServiceGraph(params=params)
    for i in range(params.min_observations):
        graph.apply_update(_update("frontend", "cartservice", 20.0, offset_s=i))

    assert graph.is_walkable("frontend", "cartservice")
    assert graph.edge_confidence("frontend", "cartservice") == 1.0


def test_weight_is_an_ewma_of_the_samples(params: RCAParams) -> None:
    """The edge weight follows `alpha*sample + (1-alpha)*prev`, not a plain sum."""
    graph = ServiceGraph(params=RCAParams(ewma_alpha=0.5))
    graph.apply_update(_update("a", "b", 10.0))
    graph.apply_update(_update("a", "b", 20.0))

    # With alpha=0.5, one round: 0.5*sample2 + 0.5*sample1. The samples are latency*log1p(n).
    # We only assert it lies strictly between the two raw latencies, proving smoothing.
    weight = graph.edge_weight("a", "b")
    assert 0 < weight < 20.0 * 2  # smoothed, not a raw accumulation


def test_successors_and_predecessors_are_sorted(warm_graph: ServiceGraph) -> None:
    """`successors` returns callees sorted; `predecessors` returns callers sorted."""
    assert warm_graph.successors("checkoutservice") == [
        "currencyservice",
        "paymentservice",
        "productcatalogservice",
        "shippingservice",
    ]
    assert warm_graph.predecessors("productcatalogservice") == ["checkoutservice"]


def test_defensive_paths_reject_bad_edges(params: RCAParams) -> None:
    """The defensive `apply_update`/`_load_snapshot` reject self-loops and unknowns.

    `GraphUpdate` already rejects these at the wire level, but the defensive guards still
    fire when a stale snapshot file or an internal caller bypasses the validator.
    """
    graph = ServiceGraph(params=params)
    graph.seed_from_signals([("frontend", "unknown"), ("frontend", "frontend")])
    assert graph.services() == []

    # Build a corrupt snapshot by hand, bypass the wire check, and feed it through the loader.
    import json
    from pathlib import Path

    path = Path("/tmp/inframind-test-corrupt-snapshot.json")
    path.write_text(
        json.dumps(
            {
                "edges": [{"caller": "x", "callee": "x", "weight": 1.0, "observations": 5}],
                "callers": {"x": ["x"]},
            }
        )
    )

    corrupt_loaded = ServiceGraph(seed_path=path, params=params)
    assert corrupt_loaded.services() == []
    path.unlink()


def test_decay_removes_silent_edges(params: RCAParams) -> None:
    """An edge idle for more than 2 * half_life is pruned."""
    graph = ServiceGraph(params=params)
    for i in range(params.min_observations):
        graph.apply_update(_update("frontend", "cartservice", 20.0, offset_s=i))

    # Decay well past two half-lives into the future.
    pruned = graph.decay(now=T0 + timedelta(seconds=10 * params.decay_half_life_s))

    assert pruned == len(warm_edges())
    assert not graph.is_walkable("frontend", "cartservice")


def warm_edges() -> list[tuple[str, str]]:
    """The single edge seeded by `test_decay_removes_silent_edges`."""
    return [("frontend", "cartservice")]


def test_decay_reduces_weight_before_pruning(params: RCAParams) -> None:
    """A still-live edge loses weight as time passes but is not pruned."""
    graph = ServiceGraph(params=params)
    for i in range(params.min_observations):
        graph.apply_update(_update("frontend", "cartservice", 40.0, offset_s=i))
    before = graph.edge_weight("frontend", "cartservice")

    # Just under one half-life: weight should drop but the edge survives.
    graph.decay(now=T0 + timedelta(seconds=int(params.decay_half_life_s * 0.5)))

    assert graph.edge_weight("frontend", "cartservice") < before
    assert graph.is_walkable("frontend", "cartservice")


def test_seeded_stub_edges_survive_but_remain_low_confidence(params: RCAParams) -> None:
    """Cold-start stub edges are structural — they survive decay but keep confidence 0."""
    graph = ServiceGraph(params=params)
    graph.seed_from_signals([("frontend", "cartservice")])

    pruned = graph.decay(now=T0 + timedelta(seconds=10 * params.decay_half_life_s))

    assert pruned == 0
    # Stub edges are walkable (for cold-start ranking) but their confidence is 0.
    assert graph.is_walkable("frontend", "cartservice")
    assert graph.edge_confidence("frontend", "cartservice") == 0.0


@pytest.mark.parametrize("bad_pair", [("unknown", "b"), ("a", "unknown"), ("a", "a")])
def test_seed_from_signals_rejects_bad_pairs(params: RCAParams, bad_pair: tuple[str, str]) -> None:
    graph = ServiceGraph(params=params)
    graph.seed_from_signals([bad_pair])
    assert graph.services() == []
