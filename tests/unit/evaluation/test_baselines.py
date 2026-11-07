"""Baseline tests (D7): each baseline returns a valid ranking from a fixture-like input."""

from __future__ import annotations

import pytest

from inframind.evaluation.baselines import (
    BASELINE_NAMES,
    PAGERANK_METHOD,
    BaselineInput,
    DeepestErrorSpanBaseline,
    EmptyServiceSet,
    HighestErrorServiceBaseline,
    LLmOnlyBaseline,
    RandomServiceBaseline,
    RawAlertmanagerBaseline,
    extract_service,
    rank_from_pagerank,
)
from inframind.rca.graph import ServiceGraph


def _input() -> BaselineInput:
    """A three-service input that distinguishes every heuristic."""
    return BaselineInput(
        services=("cartservice", "front-end", "productcatalogservice"),
        signals_by_service={
            "cartservice": [{"attrs": {"total_count": 100, "error_count": 10}}],
            "front-end": [{"attrs": {"total_count": 100, "error_count": 90}}],
            "productcatalogservice": [{"attrs": {"total_count": 100, "error_count": 1}}],
        },
        trace_depth_by_service={
            "cartservice": 2,
            "front-end": 1,
            "productcatalogservice": 5,
        },
        alerts_by_service={"cartservice": [{"instance": "cartservice"}]},
    )


def test_baseline_names_are_the_six_d7_baselines() -> None:
    """The six names are locked by D7; adding or removing one is a D-change."""
    assert BASELINE_NAMES == (
        "raw_alertmanager",
        "random_service",
        "highest_error_service",
        "deepest_error_span",
        PAGERANK_METHOD,
        "llm_only",
    )
    assert len(BASELINE_NAMES) == 6


def test_baseline_input_rejects_empty_service_set() -> None:
    """An empty service set is a construction error, not a silent empty ranking."""
    with pytest.raises(ValueError, match="non-empty"):
        BaselineInput(services=())


def test_raw_alertmanager_ranks_by_alert_frequency() -> None:
    """The alert frequency decides the order; no graph, no correlation."""
    ranked = RawAlertmanagerBaseline().rank(_input())
    assert ranked[0] == "cartservice"
    assert sorted(ranked) == sorted(_input().services)


def test_raw_alertmanager_is_the_symptom_service() -> None:
    """Raw Alertmanager points at the alerting service, not the root cause.

    That is the point of the baseline: it is the "what do we already have" floor.
    """
    ranked = RawAlertmanagerBaseline().rank(_input())
    assert ranked == ["cartservice", "front-end", "productcatalogservice"]


def test_random_service_is_deterministic_for_a_seed() -> None:
    """The chance floor is reproducible: same seed -> same order."""
    first = RandomServiceBaseline(seed=42).rank(_input())
    second = RandomServiceBaseline(seed=42).rank(_input())
    assert first == second
    assert sorted(first) == sorted(_input().services)


def test_random_service_varies_with_seed() -> None:
    """Two different seeds should not always produce the same order."""
    orders = {tuple(RandomServiceBaseline(seed=s).rank(_input())) for s in range(20)}
    assert len(orders) > 1


def test_highest_error_service_picks_the_noisiest_service() -> None:
    """front-end has 90% errors in the fixture, so it ranks first."""
    ranked = HighestErrorServiceBaseline().rank(_input())
    assert ranked[0] == "front-end"


def test_highest_error_service_handles_zero_totals() -> None:
    """A service with no counted requests has rate 0.0, not a ZeroDivisionError."""
    empty = BaselineInput(services=("a", "b"))
    ranked = HighestErrorServiceBaseline().rank(empty)
    assert ranked == ["a", "b"]


def test_highest_error_service_ties_break_alphabetically() -> None:
    """Equal error rates resolve deterministically."""
    tied = BaselineInput(
        services=("zeta", "alpha", "mid"),
        signals_by_service={
            "zeta": [{"attrs": {"total_count": 10, "error_count": 5}}],
            "alpha": [{"attrs": {"total_count": 10, "error_count": 5}}],
            "mid": [{"attrs": {"total_count": 10, "error_count": 5}}],
        },
    )
    assert HighestErrorServiceBaseline().rank(tied) == ["alpha", "mid", "zeta"]


def test_deepest_error_span_picks_the_deepest() -> None:
    """productcatalogservice is at trace depth 5, so it ranks first."""
    ranked = DeepestErrorSpanBaseline().rank(_input())
    assert ranked[0] == "productcatalogservice"


def test_deepest_error_span_defaults_missing_depth_to_zero() -> None:
    """A service with no trace record sits at depth 0 and sorts last."""
    partial = BaselineInput(services=("a", "b"), trace_depth_by_service={"a": 4})
    ranked = DeepestErrorSpanBaseline().rank(partial)
    assert ranked == ["a", "b"]


def test_llm_only_picks_the_service_it_names() -> None:
    """The LLM's reply decides the top of the ranking."""
    ranked = LLmOnlyBaseline().rank(_input(), summary_text="The root cause is cart-service.")
    assert ranked[0] == "cartservice"


def test_llm_only_falls_back_when_the_summary_names_nothing_known() -> None:
    """An off-vocabulary reply sorts alphabetically rather than inventing a pick."""
    ranked = LLmOnlyBaseline().rank(_input(), summary_text="Something unrelated went wrong.")
    assert ranked == sorted(_input().services)


def test_llm_only_with_no_summary_is_alphabetical() -> None:
    """No summary -> alphabetical order; the baseline refuses to guess."""
    ranked = LLmOnlyBaseline().rank(_input(), summary_text="")
    assert ranked == sorted(_input().services)


def test_extract_service_prefers_a_known_name() -> None:
    """`extract_service` returns the first known service mentioned."""
    known = frozenset({"cartservice", "front-end"})
    assert extract_service("I think front-end is at fault", known) == "front-end"
    assert extract_service("nothing here", known) is None


def test_extract_service_does_not_match_ordinary_words() -> None:
    """The kebab-case pattern means English prose never matches."""
    known = frozenset({"a-service"})
    assert extract_service("the service was fine", known) is None


def test_llm_only_is_not_given_the_ranker_output() -> None:
    """D1: the LLM-only baseline never sees InfraMind's ranking.

    This is the control. It ranks from the summary text alone; if it scored as well as
    InfraMind that would be a finding about the evaluation, not a reason to change the code.
    """
    baseline_input = _input()
    with_summary = LLmOnlyBaseline().rank(baseline_input, summary_text="cartservice")
    assert with_summary[0] == "cartservice"
    # No `ranked_hints` populated means the baseline had nothing from the ranker to lean on.
    assert baseline_input.ranked_hints == ()


def test_rank_from_pagerank_is_a_thin_adapter() -> None:
    """`rank_from_pagerank` delegates to the P5 implementation rather than reimplementing it."""
    from datetime import UTC, datetime

    graph = ServiceGraph(seed_path=None)
    anomalies = []
    symptoms = ["cartservice"]
    window = (datetime(2026, 10, 21, 12, 0, tzinfo=UTC), datetime(2026, 10, 21, 12, 3, tzinfo=UTC))
    ranked = rank_from_pagerank(symptoms=symptoms, anomalies=anomalies, graph=graph, window=window)
    # The symptom service is included as a candidate even on a cold-start graph
    # (D2: the walker sees its own starting point, just at depth 0).
    assert ranked[0] == "cartservice"
    assert all(isinstance(s, str) for s in ranked)


def test_every_baseline_refuses_an_empty_ranking_request() -> None:
    """Each baseline raises `EmptyServiceSet` rather than returning an empty list."""
    for factory in (
        lambda: RawAlertmanagerBaseline(),
        lambda: RandomServiceBaseline(),
        lambda: HighestErrorServiceBaseline(),
        lambda: DeepestErrorSpanBaseline(),
    ):
        baseline = factory()
        empty = BaselineInput.__new__(BaselineInput)
        # Build an "empty" input bypassing __post_init__ only for this negative test.
        object.__setattr__(empty, "services", ())
        object.__setattr__(empty, "signals_by_service", {})
        object.__setattr__(empty, "trace_depth_by_service", {})
        object.__setattr__(empty, "alerts_by_service", {})
        object.__setattr__(empty, "summary_text", "")
        object.__setattr__(empty, "ranked_hints", ())
        with pytest.raises(EmptyServiceSet):
            baseline.rank(empty)
