"""Shared fixtures for the RCA unit tests.

The topology mirrors the real testbed (Online Boutique): `frontend` calls `cartservice` and
`checkoutservice`; `checkoutservice` fans out to `paymentservice`, `currencyservice`,
`productcatalogservice`, and `shippingservice`. Edge direction is `caller -> callee` (D2), so
the deepest services in that fan-out are the plausible root causes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from inframind.common.anomaly import Anomaly
from inframind.common.graph_update import GraphUpdate
from inframind.common.signal import Severity
from inframind.rca.config import RCAParams
from inframind.rca.graph import ServiceGraph

# The real testbed topology (D2: caller -> callee).
BOUTIQUE_EDGES: list[tuple[str, str]] = [
    ("frontend", "cartservice"),
    ("frontend", "checkoutservice"),
    ("checkoutservice", "paymentservice"),
    ("checkoutservice", "currencyservice"),
    ("checkoutservice", "productcatalogservice"),
    ("checkoutservice", "shippingservice"),
]

T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def params() -> RCAParams:
    """Default params; `min_observations=3` so tests must feed an edge 3x to make it walkable."""
    return RCAParams()


@pytest.fixture
def warm_graph(params: RCAParams) -> ServiceGraph:
    """A fully-walkable graph: every edge observed `min_observations` times."""
    graph = ServiceGraph(params=params)
    for caller, callee in BOUTIQUE_EDGES:
        for i in range(params.min_observations):
            graph.apply_update(
                GraphUpdate(
                    caller=caller,
                    callee=callee,
                    latency_ms=20.0 + i,
                    observed_at=T0 + timedelta(seconds=i),
                )
            )
    return graph


def make_anomaly(
    service: str,
    *,
    score: float = 3.0,
    severity: Severity = Severity.CRITICAL,
    detector: str = "zscore",
    offset_s: float = 0.0,
    signal_id: str | None = None,
) -> Anomaly:
    """Build one `Anomaly` at `T0 + offset_s`."""
    return Anomaly(
        ts=T0 + timedelta(seconds=offset_s),
        detector=detector,
        service=service,
        signal_id=signal_id or f"sig-{service}-{offset_s}",
        score=score,
        severity=severity,
        attrs={"frozen": False},
    )


@pytest.fixture
def incident_window() -> tuple[datetime, datetime]:
    """The (opened_at, closed_at) window the anomalies in most tests fall inside."""
    return (T0, T0 + timedelta(minutes=5))
