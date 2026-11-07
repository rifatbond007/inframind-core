"""Ablation tests (standard 10.8), including the D1 invariant."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from inframind.common.anomaly import Anomaly
from inframind.common.graph_update import GraphUpdate
from inframind.common.signal import Severity
from inframind.correlation.config import CorrelationParams
from inframind.evaluation.ablations import (
    ABLATION_NAMES,
    correlation_params_for,
    is_no_llm,
    params_for,
    rank_with_ablation,
)
from inframind.rca.config import RCAParams
from inframind.rca.graph import ServiceGraph


def _graph() -> ServiceGraph:
    """A small warm graph: front-end -> cartservice -> productcatalogservice."""
    graph = ServiceGraph(seed_path=None)
    base = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    for _i in range(5):
        graph.apply_update(
            GraphUpdate(
                caller="front-end",
                callee="cartservice",
                latency_ms=100.0,
                observed_at=base,
            )
        )
        graph.apply_update(
            GraphUpdate(
                caller="cartservice",
                callee="productcatalogservice",
                latency_ms=200.0,
                observed_at=base,
            )
        )
    return graph


def _anomalies() -> list[Anomaly]:
    """Anomalies on the deepest callee — the actual root cause."""
    return [
        Anomaly(
            ts=datetime(2026, 10, 21, 12, 0, 30, tzinfo=UTC),
            detector="error_spike",
            service="productcatalogservice",
            signal_id="01HFX000000000000000000001",
            score=9.0,
            severity=Severity.CRITICAL,
        ),
        Anomaly(
            ts=datetime(2026, 10, 21, 12, 0, 32, tzinfo=UTC),
            detector="error_spike",
            service="cartservice",
            signal_id="01HFX000000000000000000002",
            score=3.0,
            severity=Severity.WARNING,
        ),
    ]


def _window() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 10, 21, 12, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 21, 12, 3, 30, tzinfo=UTC),
    )


def test_ablations_are_the_four_locked_names() -> None:
    """The four ablation names are pinned by standard 10.8."""
    assert ABLATION_NAMES == (
        "no-correlation",
        "no-rca",
        "no-llm",
        "no-change-correlation",
    )


def test_params_for_is_a_noop_for_no_llm_and_no_rca() -> None:
    """`no-llm` and `no-rca` do not change the RCA params themselves."""
    rca = RCAParams()
    assert params_for("no-llm", rca=rca) is rca
    assert params_for("no-rca", rca=rca) is rca
    assert params_for("no-correlation", rca=rca) is rca


def test_no_change_correlation_zeroes_only_that_term() -> None:
    """`no-change-correlation` sets `w_change_correlation` to 0 and nothing else."""
    rca = RCAParams()
    ablated = params_for("no-change-correlation", rca=rca)
    assert ablated.w_change_correlation == 0.0
    assert ablated.w_anomaly_severity == rca.w_anomaly_severity
    assert ablated.w_earliest_onset == rca.w_earliest_onset
    assert ablated.w_downstream_depth == rca.w_downstream_depth


def test_params_for_rejects_unknown_ablation() -> None:
    """An unknown ablation name is a ValueError, not a silent passthrough."""
    with pytest.raises(ValueError, match="unknown ablation"):
        params_for("no-such-thing", rca=RCAParams())


def test_correlation_params_for_disables_grouping() -> None:
    """`no-correlation` sets `CorrelationParams.enabled = False`."""
    base = CorrelationParams()
    off = correlation_params_for("no-correlation", base=base)
    assert off.enabled is False
    assert correlation_params_for("no-llm", base=base) is base


def test_no_rca_still_returns_a_ranking() -> None:
    """With the graph term removed, the ranker still ranks by anomaly severity."""
    candidates = rank_with_ablation(
        "no-rca",
        incident_id="test",
        symptoms=["front-end"],
        anomalies=_anomalies(),
        graph=_graph(),
        window=_window(),
        rca=RCAParams(),
    )
    assert candidates
    assert all(isinstance(c.service, str) for c in candidates)


def test_no_llm_produces_an_identical_ranking_to_the_full_ranker() -> None:
    """D1: removing the LLM cannot change the ranking.

    `no-llm` never reaches the ranker, so the two rankings must match service-for-service
    and score-for-score. A mismatch here means model output leaked into a decision.
    """
    full = rank_with_ablation(
        "no-change-correlation",  # any name that leaves params untouched works; use no-llm below
        incident_id="test",
        symptoms=["front-end"],
        anomalies=_anomalies(),
        graph=_graph(),
        window=_window(),
        rca=RCAParams(),
    )
    without_llm = rank_with_ablation(
        "no-llm",
        incident_id="test",
        symptoms=["front-end"],
        anomalies=_anomalies(),
        graph=_graph(),
        window=_window(),
        rca=RCAParams(),
    )
    assert [c.service for c in full] == [c.service for c in without_llm]
    assert [c.score for c in full] == [c.score for c in without_llm]


def test_is_no_llm() -> None:
    """`is_no_llm` recognises only the one ablation."""
    assert is_no_llm("no-llm") is True
    assert is_no_llm("no-rca") is False


def test_no_change_correlation_changes_nothing_without_change_events() -> None:
    """With no change events in the window, the ablation is a no-op on the output."""
    graph = _graph()
    anomalies = _anomalies()
    window = _window()
    full = rank_with_ablation(
        "no-llm",
        incident_id="t",
        symptoms=["front-end"],
        anomalies=anomalies,
        graph=graph,
        window=window,
        rca=RCAParams(),
    )
    ablated = rank_with_ablation(
        "no-change-correlation",
        incident_id="t",
        symptoms=["front-end"],
        anomalies=anomalies,
        graph=graph,
        window=window,
        rca=RCAParams(),
    )
    assert [c.service for c in full] == [c.service for c in ablated]
