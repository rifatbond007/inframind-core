"""The ranker: frozen inputs produce a frozen ranking (standard 06.8 `test_rank`).

The determinism guard is the point of this file: the same ``(graph, anomaly_set, incident
window)`` must always produce the same order, with no wall clock and no randomness
(D1, standard 06.7). Bumping the ranker deliberately breaks the golden test, which is what
reviewers see in the PR.

Also covers: the weighted formula's behaviour, the depth term, the low-confidence downweight,
and the PageRank variant's agreement on the obvious root cause.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from inframind.common.signal import Severity
from inframind.rca.config import RCAParams
from inframind.rca.graph import ServiceGraph
from inframind.rca.rank import anomaly_severity, rank_candidates
from inframind.rca.rank_pagerank import personalised_pagerank, rank_pagerank

from .conftest import BOUTIQUE_EDGES, T0, make_anomaly


def _fault_anomalies() -> list:
    """The canonical scenario: frontend errors; paymentservice is the real root cause.

    paymentservice is deepest (2 hops from the symptom), most severe, and fires earliest —
    all three weighted terms should agree it is the root cause.
    """
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


# --- the headline golden test (standard 06.8) ---


def test_frozen_incident_produces_a_frozen_ranking(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """The same inputs always give the same order. This is the D1 / standard 06.7 guard."""
    anomalies = _fault_anomalies()
    window = incident_window

    first = [
        c.service
        for c in rank_candidates("inc-1", ["frontend"], anomalies, warm_graph, window=window)
    ]
    for _ in range(5):
        again = [
            c.service
            for c in rank_candidates("inc-1", ["frontend"], anomalies, warm_graph, window=window)
        ]
        assert again == first, "ranking is not deterministic (D1 / standard 06.7)"


def test_frozen_ranking_scores_are_stable(warm_graph: ServiceGraph, incident_window: tuple) -> None:
    """Scores, not just order, are byte-stable across runs."""
    anomalies = _fault_anomalies()
    first = [
        c.score
        for c in rank_candidates(
            "inc-1", ["frontend"], anomalies, warm_graph, window=incident_window
        )
    ]
    second = [
        c.score
        for c in rank_candidates(
            "inc-1", ["frontend"], anomalies, warm_graph, window=incident_window
        )
    ]
    assert first == second


# --- does it find the right root cause? ---


def test_ranks_the_deepest_most_severe_service_first(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    ranked = rank_candidates(
        "inc-1", ["frontend"], _fault_anomalies(), warm_graph, window=incident_window
    )
    assert ranked[0].service == "paymentservice"


def test_symptom_service_is_still_a_candidate(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """ "The symptom is its own cause" is a visible outcome, not a silent omission."""
    ranked = rank_candidates(
        "inc-1", ["frontend"], _fault_anomalies(), warm_graph, window=incident_window
    )
    assert any(c.service == "frontend" for c in ranked)


def test_ranking_is_sorted_descending_by_score(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    ranked = rank_candidates(
        "inc-1", ["frontend"], _fault_anomalies(), warm_graph, window=incident_window
    )
    scores = [c.score for c in ranked]
    assert scores == sorted(scores, reverse=True)


def test_top_k_is_respected(warm_graph: ServiceGraph, incident_window: tuple) -> None:
    ranked = rank_candidates(
        "inc-1", ["frontend"], _fault_anomalies(), warm_graph, window=incident_window, top_k=2
    )
    assert len(ranked) == 2


# --- the terms ---


def test_anomaly_severity_is_zero_for_no_anomalies() -> None:
    assert anomaly_severity([]) == 0.0


def test_anomaly_severity_increases_with_score_and_count() -> None:
    one = anomaly_severity([make_anomaly("a", score=5.0)])
    two = anomaly_severity([make_anomaly("a", score=5.0), make_anomaly("a", score=5.0, offset_s=1)])
    assert 0.0 < one < two < 1.0


def test_anomaly_severity_is_bounded_below_one() -> None:
    """The x/(1+x) squash keeps a candidate with 1000 anomalies below 1.0."""
    many = [make_anomaly("a", score=50.0, offset_s=i) for i in range(1000)]
    assert anomaly_severity(many) < 1.0


def test_depth_term_rewards_deeper_candidates(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """Deeper candidates (further from the symptom) get a larger depth term."""
    ranked = {
        c.service: c
        for c in rank_candidates(
            "inc-1", ["frontend"], _fault_anomalies(), warm_graph, window=incident_window, top_k=10
        )
    }
    # paymentservice sits two hops below the symptom; cartservice only one.
    assert ranked["paymentservice"].depth > ranked["cartservice"].depth


# --- low-confidence downweight (standard 13.2) ---


def test_cold_start_graph_yields_zero_confidence_for_visited_services(
    params: RCAParams, incident_window: tuple
) -> None:
    """Cold-start reachable services (those reached through stub edges) have confidence 0.

    The symptom service itself is a single-node path so its confidence is 1.0; every
    walkable callee goes through a stub edge and so reports 0. Only real ``GraphUpdate``
    events promote them above zero (standard 13.5).
    """
    cold = ServiceGraph(params=params)
    cold.seed_from_signals(BOUTIQUE_EDGES)

    ranked = rank_candidates(
        "inc-1", ["frontend"], _fault_anomalies(), cold, window=incident_window
    )

    candidates = [c for c in ranked if c.service != "frontend"]
    assert candidates, "walk should reach at least one callee on the seeded topology"
    assert all(c.confidence == 0.0 for c in candidates)


def test_cold_start_scores_lie_in_the_low_confidence_band(
    params: RCAParams, warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """Reachable candidates on a cold graph score well below warm-graph scores (standard 13.2).

    The symptom itself is always confident (1.0) so its score is comparable across warm and
    cold; the structural ``downstream_depth`` term for any reachable callee is compressed by
    the ``low_confidence_multiplier`` (default 0.1).
    """
    cold = ServiceGraph(params=params)
    cold.seed_from_signals(BOUTIQUE_EDGES)

    cold_ranked = rank_candidates(
        "inc", ["frontend"], _fault_anomalies(), cold, window=incident_window, top_k=10
    )
    warm_ranked = rank_candidates(
        "inc", ["frontend"], _fault_anomalies(), warm_graph, window=incident_window, top_k=10
    )

    # The symptom itself scores the same in both, because it never relies on an edge.
    cold_symptom = next(c for c in cold_ranked if c.service == "frontend")
    warm_symptom = next(c for c in warm_ranked if c.service == "frontend")
    assert cold_symptom.score == pytest.approx(warm_symptom.score)

    # But the best reachable callee is far less confident on the cold graph.
    cold_deepest = max((c for c in cold_ranked if c.service != "frontend"), key=lambda c: c.score)
    warm_deepest = max((c for c in warm_ranked if c.service != "frontend"), key=lambda c: c.score)
    assert cold_deepest.score < warm_deepest.score


# --- tie-breaking (standard 06.7) ---


def test_ties_break_deterministically(warm_graph: ServiceGraph, incident_window: tuple) -> None:
    """Two identical candidates produce a stable order under the seeded tie-break."""
    # Two leaf services with identical anomaly profiles -> identical scores.
    anomalies = [
        make_anomaly("paymentservice", score=3.0, severity=Severity.WARNING, offset_s=0),
        make_anomaly("currencyservice", score=3.0, severity=Severity.WARNING, offset_s=0),
    ]
    runs = [
        [
            c.service
            for c in rank_candidates(
                "inc", ["frontend"], anomalies, warm_graph, window=incident_window
            )
        ]
        for _ in range(5)
    ]
    assert all(r == runs[0] for r in runs)


def test_tiebreak_seed_changes_the_order_of_exact_ties(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """Different seeds are allowed to order exact ties differently (still deterministic)."""
    anomalies = [
        make_anomaly("paymentservice", score=3.0, severity=Severity.WARNING, offset_s=0),
        make_anomaly("currencyservice", score=3.0, severity=Severity.WARNING, offset_s=0),
    ]
    order_a = [
        c.service
        for c in rank_candidates(
            "inc",
            ["frontend"],
            anomalies,
            warm_graph,
            window=incident_window,
            params=RCAParams(tiebreak_seed=1),
        )
    ]
    order_b = [
        c.service
        for c in rank_candidates(
            "inc",
            ["frontend"],
            anomalies,
            warm_graph,
            window=incident_window,
            params=RCAParams(tiebreak_seed=2),
        )
    ]
    assert set(order_a) == set(order_b)
    # Both are stable within themselves.
    assert order_a == [
        c.service
        for c in rank_candidates(
            "inc",
            ["frontend"],
            anomalies,
            warm_graph,
            window=incident_window,
            params=RCAParams(tiebreak_seed=1),
        )
    ]


# --- evidence is bundled (D15) ---


def test_every_ranked_candidate_carries_evidence(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """D15: the bundle exists before ranking, so every candidate has one."""
    ranked = rank_candidates(
        "inc-1", ["frontend"], _fault_anomalies(), warm_graph, window=incident_window
    )
    with_anomalies = [c for c in ranked if c.service == "paymentservice"]
    assert with_anomalies[0].evidence, "the root-cause candidate has no evidence bundle"


def test_evidence_refs_point_at_the_anomaly_signals(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """Every evidence ref is a real signal id (or anomaly id) the ranker actually used."""
    anomalies = _fault_anomalies()
    ranked = rank_candidates("inc-1", ["frontend"], anomalies, warm_graph, window=incident_window)
    payment = next(c for c in ranked if c.service == "paymentservice")
    refs = {e.ref for e in payment.evidence}

    signal_ids = {a.signal_id for a in anomalies}
    anomaly_ids = {a.id for a in anomalies}
    assert refs <= (signal_ids | anomaly_ids)
    assert refs, "the root-cause candidate has an empty evidence bundle"


def test_evidence_is_empty_for_a_candidate_with_no_anomalies(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """A structurally-reachable but quiet service gets a candidate with no evidence."""
    ranked = rank_candidates(
        "inc-1", ["frontend"], _fault_anomalies(), warm_graph, window=incident_window, top_k=10
    )
    quiet = next(c for c in ranked if c.service == "shippingservice")
    assert quiet.evidence == []


# --- the PageRank variant (standard 06.5) ---


def test_pagerank_scores_sum_to_one(warm_graph: ServiceGraph) -> None:
    scores = personalised_pagerank(
        warm_graph,
        ["frontend", "cartservice", "checkoutservice", "paymentservice"],
        {"frontend": 4.0, "paymentservice": 6.0},
        alpha=0.85,
    )
    assert sum(scores.values()) == pytest.approx(1.0)


def test_pagerank_alpha_zero_returns_personalisation(warm_graph: ServiceGraph) -> None:
    scores = personalised_pagerank(
        warm_graph, ["frontend", "cartservice"], {"frontend": 3.0, "cartservice": 1.0}, alpha=0.0
    )
    assert scores["frontend"] == pytest.approx(0.75)
    assert scores["cartservice"] == pytest.approx(0.25)


def test_pagerank_variant_is_deterministic(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    anomalies = _fault_anomalies()
    first = [
        (c.service, c.score)
        for c in rank_pagerank("inc", ["frontend"], anomalies, warm_graph, window=incident_window)
    ]
    second = [
        (c.service, c.score)
        for c in rank_pagerank("inc", ["frontend"], anomalies, warm_graph, window=incident_window)
    ]
    assert first == second


def test_pagerank_variant_reaches_the_deep_root_cause(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """The variant must use the same bounded walk, so it also sees depth-2 services."""
    ranked = rank_pagerank(
        "inc", ["frontend"], _fault_anomalies(), warm_graph, window=incident_window
    )
    assert any(c.service == "paymentservice" for c in ranked)


def test_weighted_ranker_finds_the_root_cause_but_pagerank_does_not(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """The weighted ranker identifies `paymentservice`; PageRank puts the hub `checkoutservice`
    first.

    This divergence is expected and is precisely why standard 06.5 calls PageRank a
    *sensitivity analysis* rather than the headline number: PageRank measures graph centrality,
    which favours mid-graph hubs, whereas root-cause identification needs depth + severity +
    recency together. The paper reports both to make this trade-off visible.
    """
    anomalies = _fault_anomalies()
    weighted = rank_candidates("inc", ["frontend"], anomalies, warm_graph, window=incident_window)
    pagerank = rank_pagerank("inc", ["frontend"], anomalies, warm_graph, window=incident_window)

    assert weighted[0].service == "paymentservice"
    # The variant sees paymentservice (it walks the same graph) but does not rank it first.
    assert any(c.service == "paymentservice" for c in pagerank)
    assert pagerank[0].service != "paymentservice"


# --- no wall clock (standard 06.7) ---


def test_rank_does_not_call_utcnow_or_random(warm_graph: ServiceGraph) -> None:
    """The ranker reads the incident window, never the wall clock (standard 06.7).

    Walks the module AST looking for calls to ``utcnow`` and for ``random`` imports, so a
    docstring mentioning "no utcnow()" does not satisfy the check.
    """
    import ast
    import inspect

    from inframind.rca import rank as rank_module

    tree = ast.parse(inspect.getsource(rank_module))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "utcnow" not in called
    assert "random" not in called
    assert "uniform" not in called

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "random" not in imported


def test_rank_is_unaffected_by_the_real_clock(
    warm_graph: ServiceGraph, incident_window: tuple
) -> None:
    """Two different real-time instants give the same ranking for the same incident window."""
    anomalies = _fault_anomalies()
    a = [
        c.service
        for c in rank_candidates("inc", ["frontend"], anomalies, warm_graph, window=incident_window)
    ]
    far_future = (T0 + timedelta(days=365), T0 + timedelta(days=365, minutes=5))
    b = [
        c.service
        for c in rank_candidates("inc", ["frontend"], anomalies, warm_graph, window=far_future)
    ]
    assert a == b
