"""The default deterministic RCA ranker (D1, standard 06.4).

The headline score is a transparent weighted sum::

    score(s) = w1 * anomaly_severity
             + w2 * earliest_onset
             + w3 * downstream_depth
             + w4 * change_correlation

The four weights sum to 1 and are tuned on the **dev split only** (D6). The PageRank variant in
``rank_pagerank.py`` is reported side by side as a sensitivity analysis; this formula is
InfraMind's headline number (standard 06.5).

Hard rules enforced here:

* **D1** — ranking is deterministic. No ``random``, no wall clock. The same
  ``(graph, anomaly_set, change_events)`` always produces the same order.
* **D2** — the walk already moved toward callees; this module only scores what it was given.
* **Standard 06.7** — no ``utcnow()``: every time input comes from the incident.
* **Standard 13.2** — a low-confidence edge contributes a downweighted ``downstream_depth``,
  so a cold-start (zero-confidence) graph cannot outrank a warm one on graph structure alone.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from inframind.common.anomaly import Anomaly

from .change_correlation import ChangeEvent, change_correlation
from .config import RCAParams
from .evidence import (
    Candidate,
    anomalies_by_service,
    build_evidence,
    changes_by_service,
    downstream_depth,
    earliest_onset,
)
from .graph import ServiceGraph
from .traversal import Candidate as WalkCandidate
from .traversal import walk_toward_callees


def anomaly_severity(anomalies: Sequence[Anomaly]) -> float:
    """Return the ``anomaly_severity`` term, normalised into [0, 1] (standard 06.4).

    Severity is the sum of detector scores for this candidate in the incident window, weighted
    by the detector's severity band (critical > warning > info). The sum is squashed through
    ``x / (1 + x)`` so a candidate with many weak anomalies does not automatically beat one with
    a single decisive anomaly.
    """
    if not anomalies:
        return 0.0
    band = {"info": 0.5, "warning": 0.75, "critical": 1.0}
    total = sum(band.get(a.severity, 1.0) * max(0.0, float(a.score)) for a in anomalies)
    return total / (1.0 + total)


def _tiebreak_key(service: str, seed: int) -> str:
    """Deterministic tie-break key: a stable hash of ``seed`` and the service name.

    Ties are broken by this key so equal scores still yield a stable order across runs and
    Python versions (standard 06.7). It is a hash, not ``random`` — no state, no seeding.
    """
    digest = hashlib.blake2b(f"{seed}:{service}".encode(), digest_size=8).hexdigest()
    return digest


def rank_candidates(
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
    """Rank candidate root causes for one incident.

    For each symptom service, walk toward callees (D2), collect that candidate's anomalies and
    change events, bundle the evidence (D15), then score:

    1. Walk every symptom to its reachable candidates, bounded by ``max_depth``.
    2. For each candidate, gather anomalies and change events in the incident window.
    3. Score with the weighted formula; the ``w4`` term calls ``change_correlation``.
    4. Return the top ``top_k`` candidates, best first, ties broken deterministically.

    A candidate that is a symptom service scores ``downstream_depth = 0`` and is included so
    "the symptom is its own cause" is a visible outcome rather than a silent omission.
    """
    params = params or RCAParams()
    limit = top_k if top_k is not None else params.top_k
    opened_at, _closed_at = window

    grouped_anomalies = anomalies_by_service(anomalies)
    grouped_changes = changes_by_service(change_events)

    # Deduplicate candidates reached from more than one symptom, keeping the shallowest depth.
    depths: dict[str, int] = {}
    paths: dict[str, tuple[str, ...]] = {}
    for symptom in symptoms:
        for reached in walk_toward_callees(graph, symptom, params=params):
            existing = depths.get(reached.service)
            if existing is None or reached.depth < existing:
                depths[reached.service] = reached.depth
                paths[reached.service] = reached.path

    scored: list[Candidate] = []
    for service in sorted(depths):
        depth = depths[service]
        service_anomalies = grouped_anomalies.get(service, [])
        service_changes = grouped_changes.get(service, [])

        severity_term = anomaly_severity(service_anomalies)
        onset_term = earliest_onset(service_anomalies, opened_at)
        depth_term = downstream_depth(depth, params.max_depth)
        change_term = change_correlation(service, window, change_events)

        # Downweight by graph confidence so a cold-start (zero-confidence) topology cannot
        # dominate the ranking on structure alone (standard 13.2).
        confidence = _graph_confidence(graph, paths[service])

        raw_score = (
            params.w_anomaly_severity * severity_term
            + params.w_earliest_onset * onset_term
            + params.w_downstream_depth * depth_term
            + params.w_change_correlation * change_term
        )
        score = raw_score * (1.0 - (1.0 - confidence) * (1.0 - params.low_confidence_multiplier))

        from .evidence import CandidateBundle  # local import avoids a cycle at module load

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
                confidence=confidence,
                evidence=evidence,
            )
        )

    scored.sort(key=lambda c: (-c.score, _tiebreak_key(c.service, params.tiebreak_seed)))
    return scored[:limit]


def _graph_confidence(graph: ServiceGraph, path: Sequence[str]) -> float:
    """Mean edge confidence along `path`; 1.0 for the single-node (symptom) path."""
    if len(path) < 2:
        return 1.0
    pairs = [(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(graph.edge_confidence(caller, callee) for caller, callee in pairs)
    return total / (len(path) - 1)


__all__ = [
    "Candidate",
    "RCAParams",
    "WalkCandidate",
    "anomaly_severity",
    "rank_candidates",
]
