"""Personalised PageRank variant of the ranker (MicroRCA-style, standard 06.5).

`Personalised PageRank` is the ``restart-with-personalisation`` variant: with probability ``alpha``
the random walker follows an outgoing edge, with probability ``1 - alpha`` it teleports back to
a service drawn from a personalisation vector (here: the per-service anomaly score).

We implement it natively as power iteration (no NetworkX dependency). The walker is restricted
to *walkable* edges only — the same edge confidence threshold the walk uses — so a cold-start
graph cannot cheat the variant either.

The headline number reported in the paper is the weighted-formula ranker in `rank.py`. This
variant is the sensitivity analysis (standard 06.5).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from inframind.common.anomaly import Anomaly
from inframind.common.signal import normalise_service

from .change_correlation import ChangeEvent, change_correlation
from .config import RCAParams
from .evidence import (
    Candidate,
    anomalies_by_service,
    build_evidence,
    changes_by_service,
)
from .graph import ServiceGraph
from .rank import anomaly_severity
from .traversal import walk_toward_callees


def personalised_pagerank(
    graph: ServiceGraph,
    services: Sequence[str],
    personalisation: dict[str, float],
    *,
    alpha: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-9,
) -> dict[str, float]:
    """Run personalised PageRank restricted to ``services`` (a sub-graph induced by nodes).

    Returns one score per input service. The personalisation vector and the service list must
    cover the same keys (services with zero personalisation never receive teleport probability).

    Restrictions:

    * Only ``graph.is_walkable(caller, callee)`` edges are followed. Under-observed edges have
      the same confidence-0 penalty here that the walk sees.
    * ``alpha == 0`` collapses to the personalisation vector (every teleport, no walk).
    * ``alpha == 1`` collapses to random walk without teleport: degree-weighted stationary
      distribution on the walkable subgraph.

    The iteration converges when the L1 delta between successive score vectors falls below
    ``tol``, or after ``max_iter`` iterations — whichever comes first.
    """
    services_set = {normalise_service(s) for s in services}
    raw_pers = {normalise_service(s): float(personalisation.get(s, 0.0)) for s in services_set}
    total = sum(raw_pers.values())
    if total > 0:
        pers = {k: v / total for k, v in raw_pers.items()}
    else:
        # Fall back to a uniform personalisation if no anomalies were provided.
        pers = {k: 1.0 / len(services_set) for k in services_set} if services_set else {}

    # Build the subgraph (walkable, the only kind we want to use for ranking).
    edges: dict[str, list[str]] = {s: [] for s in services_set}
    for service in services_set:
        for callee in graph.successors(service):
            if callee in services_set and graph.is_walkable(service, callee):
                edges[service].append(callee)

    # Dangling nodes (no walkable outgoing edges) absorb their mass uniformly across services.
    dangling: set[str] = {s for s, out in edges.items() if not out}

    scores = dict(pers)
    for _ in range(max_iter):
        next_scores: dict[str, float] = {s: 0.0 for s in services_set}
        # Walk contribution: each outgoing edge of `service` receives `alpha * scores[service] / out_degree`.
        for service, out in edges.items():
            if not out:
                continue
            share = alpha * scores[service] / len(out)
            for callee in out:
                next_scores[callee] += share
        # Dangling-node contribution: redistribute their mass uniformly (MicroRCA trick).
        dangling_total = sum(scores[s] for s in dangling) * alpha / max(1, len(services_set))
        for service in services_set:
            next_scores[service] += dangling_total
        # Teleport contribution.
        for service in services_set:
            next_scores[service] += (1.0 - alpha) * pers.get(service, 0.0)

        delta = sum(abs(next_scores[s] - scores[s]) for s in services_set)
        scores = next_scores
        if delta < tol:
            break

    return scores


def rank_pagerank(
    incident_id: str,
    symptoms: Sequence[str],
    anomalies: Sequence[Anomaly],
    graph: ServiceGraph,
    change_events: Sequence[ChangeEvent] = (),
    *,
    window: tuple[datetime, datetime],
    params: RCAParams | None = None,
    top_k: int | None = None,
) -> list[Candidate]:
    """Return the PageRank-variant rank (standard 06.5).

    Identical inputs as `rank.rank_candidates`. Symptoms act as the personalisation seeds via
    their per-service anomaly score; the PageRank scores over the walkable subgraph induced by
    every service reached from a symptom.

    The output ``Candidate.score`` is normalised into [0, 1] (sum-to-one) so the variant scores
    are directly comparable against the weighted-formula rank on the same scale. ``change_term``
    is included for parity with the formula ranker, but the headline number is the PageRank
    value — the variant's reportable claim is correlation, not causality.
    """
    params = params or RCAParams()
    limit = top_k if top_k is not None else params.top_k
    _opened_at, _closed_at = window

    # Use the same bounded walk as the weighted ranker; otherwise the PageRank variant sees
    # only one hop and cannot find the actual root cause at depth > 1.
    reachable: set[str] = set()
    for symptom in symptoms:
        for reached in walk_toward_callees(graph, symptom, params=params):
            reachable.add(reached.service)

    if not reachable:
        return []

    per_service_anomaly = {
        service: anomaly_severity(items)
        for service, items in anomalies_by_service(anomalies).items()
    }

    pagerank_scores = personalised_pagerank(
        graph,
        sorted(reachable),
        per_service_anomaly,
        alpha=params.pagerank_alpha,
    )

    scored: list[Candidate] = []
    for service in sorted(reachable):
        score = float(pagerank_scores.get(service, 0.0))
        depth = _depth_to(graph, service, symptoms)
        service_anomalies = anomalies_by_service(anomalies).get(service, [])
        service_changes = changes_by_service(change_events).get(service, [])

        change_term = change_correlation(service, window, change_events)

        from .evidence import CandidateBundle  # local import: avoid module cycle

        bundle = CandidateBundle(
            service=service,
            depth=depth,
            anomalies=service_anomalies,
            changes=service_changes,
        )
        evidence = build_evidence(bundle, window=window)

        scored.append(
            Candidate(
                service=service,
                score=score,
                depth=depth,
                change_correlation=change_term,
                confidence=(_graph_confidence(graph, symptoms, service)),
                evidence=evidence,
            )
        )

    scored.sort(key=lambda c: (-c.score, c.service))
    return scored[:limit]


def _depth_to(graph: ServiceGraph, target: str, symptoms: Sequence[str], max_depth: int = 4) -> int:
    """Shortest walkable depth from any symptom to `target`. 0 if `target` is a symptom."""
    target_n = normalise_service(target)
    if any(target_n == normalise_service(s) for s in symptoms):
        return 0
    frontier = [(normalise_service(s), 0) for s in symptoms]
    visited = {s for s, _ in frontier}
    while frontier:
        service, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for callee in graph.successors(service):
            if not graph.is_walkable(service, callee):
                continue
            callee_n = normalise_service(callee)
            if callee_n == target_n:
                return depth + 1
            if callee_n in visited:
                continue
            visited.add(callee_n)
            frontier.append((callee_n, depth + 1))
    return max_depth


def _graph_confidence(graph: ServiceGraph, symptoms: Sequence[str], service: str) -> float:
    """Confidence: mean over walkable edges from the nearest symptom to `service`.

    A simpler surrogate than the path-aware confidence used by `rank.py` — sufficient for the
    variant, which is reported for sensitivity rather than as the headline.
    """
    target_n = normalise_service(service)
    edges_conf: list[float] = []
    for symptom in symptoms:
        s_n = normalise_service(symptom)
        edges_conf.append(graph.edge_confidence(s_n, target_n))
        for callee in graph.successors(s_n):
            if normalise_service(callee) == target_n and graph.is_walkable(s_n, callee):
                edges_conf.append(graph.edge_confidence(s_n, callee))
    if not edges_conf:
        return 1.0 if any(target_n == normalise_service(s) for s in symptoms) else 0.0
    return sum(edges_conf) / len(edges_conf)


__all__ = ["personalised_pagerank", "rank_pagerank"]
