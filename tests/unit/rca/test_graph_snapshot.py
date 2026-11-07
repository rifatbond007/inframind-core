"""Snapshot -> reload round-trip preserves edges and weights (standard 13.9).

Covers: the snapshot file round-trip, and the corrupt-snapshot path (log
``ERROR graph_snapshot_unreadable`` + cold start, never crash).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from inframind.common.graph_update import GraphUpdate
from inframind.rca.config import RCAParams
from inframind.rca.graph import ServiceGraph

from .conftest import BOUTIQUE_EDGES, T0


def test_snapshot_round_trip_preserves_edges(tmp_path, params: RCAParams) -> None:
    """persist() then reload() keeps every walkable edge with its weight."""
    path = tmp_path / "service-graph.json"
    graph = ServiceGraph(seed_path=path, params=params)
    for caller, callee in BOUTIQUE_EDGES:
        for i in range(params.min_observations):
            graph.apply_update(
                GraphUpdate(
                    caller=caller,
                    callee=callee,
                    latency_ms=15.0,
                    observed_at=T0 + timedelta(seconds=i),
                )
            )
    graph.persist()

    reloaded = ServiceGraph(seed_path=path, params=params)
    for caller, callee in BOUTIQUE_EDGES:
        assert reloaded.is_walkable(caller, callee), f"{caller}->{callee} lost across reload"
        assert reloaded.edge_weight(caller, callee) == pytest.approx(
            graph.edge_weight(caller, callee)
        )


def test_reloaded_graph_keeps_observations_count(tmp_path, params: RCAParams) -> None:
    path = tmp_path / "graph.json"
    graph = ServiceGraph(seed_path=path, params=params)
    for i in range(5):
        graph.apply_update(
            GraphUpdate(
                caller="a", callee="b", latency_ms=10.0, observed_at=T0 + timedelta(seconds=i)
            )
        )
    graph.persist()

    reloaded = ServiceGraph(seed_path=path, params=params)
    assert reloaded.is_walkable("a", "b")


def test_missing_snapshot_cold_starts_without_raising(tmp_path, params: RCAParams) -> None:
    """A missing seed file logs and cold-starts; it never raises (standard 13.6)."""
    graph = ServiceGraph(seed_path=tmp_path / "does-not-exist.json", params=params)
    assert graph.services() == []
    assert graph.version() == 0


def test_corrupt_snapshot_logs_and_cold_starts(tmp_path, params: RCAParams) -> None:
    """A corrupt snapshot falls back to cold start and never crashes the worker."""
    path = tmp_path / "corrupt.json"
    path.write_text("{ this is not json ]")

    graph = ServiceGraph(seed_path=path, params=params)  # must not raise

    assert graph.services() == []
    assert graph.version() == 0


def test_snapshot_without_edges_key_is_treated_as_corrupt(tmp_path, params: RCAParams) -> None:
    """A structurally-wrong (but parseable) snapshot is also treated as unreadable."""
    path = tmp_path / "wrong-shape.json"
    path.write_text('{"unexpected_key": 123}')

    graph = ServiceGraph(seed_path=path, params=params)

    assert graph.services() == []


def test_snapshot_excludes_updated_at_and_version(params: RCAParams) -> None:
    """`updated_at` and `version` are recomputed on load, so they are not persisted."""
    graph = ServiceGraph(params=params)
    graph.apply_update(GraphUpdate(caller="a", callee="b", latency_ms=10.0, observed_at=T0))
    snapshot = graph.snapshot()

    assert "updated_at" not in snapshot
    assert "version" not in snapshot
    assert "edges" in snapshot
