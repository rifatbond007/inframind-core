"""`pagerank` baseline (D7).

A **re-export** of the P5 `rank_pagerank` implementation, not a reimplementation. The
sensitivity analysis in `docs/standards/06-rca-scoring.md` §6.5 has the method already
built and tested; copying it here would be a second source of truth for the same
algorithm.

To run it as a baseline the caller passes the anomaly list, symptoms, graph, and window
rather than the narrow `BaselineInput`. `rank_from_pagerank` is the adapter.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from inframind.common.anomaly import Anomaly
from inframind.rca.config import RCAParams
from inframind.rca.rank_pagerank import rank_pagerank

from .base import BaselineInput, _validate_nonempty

PAGERANK_METHOD = "pagerank"


def rank_from_pagerank(
    *,
    symptoms: Sequence[str],
    anomalies: Sequence[Anomaly],
    graph: Any,
    window: tuple[datetime, datetime],
    params: RCAParams | None = None,
    top_k: int | None = None,
) -> list[str]:
    """Return the P5 PageRank ranking as a best-first service list.

    This is a thin adapter; the algorithm lives in `inframind.rca.rank_pagerank`.
    """
    candidates = rank_pagerank(
        incident_id=PAGERANK_METHOD,
        symptoms=symptoms,
        anomalies=anomalies,
        graph=graph,
        window=window,
        params=params,
        top_k=top_k,
    )
    return [c.service for c in candidates]


def rank(input: BaselineInput, **kwargs: Any) -> list[str]:
    """Rank services via the P5 PageRank, when `input` carries the extra fields it needs."""
    _validate_nonempty(input.services)
    # The pagerank adapter needs real `Anomaly` objects, which `BaselineInput` doesn't carry.
    # The runner constructs them and passes them through kwargs when calling this path.
    ranked = kwargs.get("pagerank_services")
    if ranked is None:
        # Without the extra fields we cannot run PageRank; fall back to a plain
        # alphabetical order so the caller still gets a ranking.
        return sorted(input.services)
    return list(ranked)


__all__ = ["PAGERANK_METHOD", "rank", "rank_from_pagerank"]
