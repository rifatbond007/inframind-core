"""Scoring tests: top-1 / top-3 / MRR / latency on hand-computed inputs."""

from __future__ import annotations

import pytest

from inframind.evaluation.schema import ChaosSpec, ResultRow, ScenarioSpec
from inframind.evaluation.scoring import default_scorer, score, score_row


def _spec(scenario_id: str = "dev-x", *, expected: str = "cartservice") -> ScenarioSpec:
    return ScenarioSpec(
        scenario_id=scenario_id,
        set="dev",
        seed=1001,
        fault_type="pod_kill",
        target_service=expected,
        target_namespace="boutique",
        duration_s=180.0,
        warmup_s=60.0,
        cooldown_s=60.0,
        expected_root_cause=expected,
        expected_affected_services=(expected,),
        chaos=ChaosSpec(kind="PodChaos", action="pod-kill", mode="one"),
        metrics_to_assert=("http_requests_total",),
        repeats=5,
    )


def _row(**overrides: object) -> ResultRow:
    base = {
        "scenario_id": "dev-x",
        "seed": 1001,
        "repeat": 0,
        "method": "infra",
        "method_kind": "infra",
        "detected": True,
        "top1_correct": False,
        "top3_correct": False,
        "reciprocal_rank": 0.0,
        "rank": 0,
        "detection_latency_s": 30.0,
        "false_positive": False,
        "scenario_set": "dev",
        "run_id": "dev-x@1001r0:infra",
        "expected_root_cause": "cartservice",
        "predicted_root_cause": "cartservice",
    }
    base.update(overrides)
    return ResultRow(**base)  # type: ignore[arg-type]


def test_score_row_top1_hit() -> None:
    """A rank-1 hit sets top1, top3, rank=1, MRR=1.0."""
    row = _row(predicted_root_cause="cartservice|front-end|cartservice")
    scored = score_row(row)
    assert scored.top1_correct is True
    assert scored.top3_correct is True
    assert scored.rank == 1
    assert scored.reciprocal_rank == 1.0
    assert scored.predicted_root_cause == "cartservice"


def test_score_row_top3_hit_at_rank_two() -> None:
    """A rank-2 hit is top1=False, top3=True, MRR=0.5."""
    row = _row(predicted_root_cause="front-end|cartservice|checkout")
    scored = score_row(row)
    assert scored.top1_correct is False
    assert scored.top3_correct is True
    assert scored.rank == 2
    assert scored.reciprocal_rank == pytest.approx(0.5)


def test_score_row_miss_outside_top3() -> None:
    """A miss outside the top 3 is top1=False, top3=False, MRR=0.0."""
    row = _row(predicted_root_cause="a|b|c|d|cartservice")
    scored = score_row(row)
    assert scored.top1_correct is False
    assert scored.top3_correct is False
    assert scored.rank == 5
    assert scored.reciprocal_rank == pytest.approx(0.2)


def test_score_row_no_detection_is_all_miss() -> None:
    """An undetected run scores zero across the board."""
    row = _row(detected=False, predicted_root_cause=None, detection_latency_s=None)
    scored = score_row(row)
    assert scored.top1_correct is False
    assert scored.top3_correct is False
    assert scored.rank == 0
    assert scored.reciprocal_rank == 0.0
    assert scored.predicted_root_cause is None


def test_score_aggregates_top1_top3_mrr() -> None:
    """The aggregate is the mean over rows for one (set, method)."""
    rows = [
        _row(
            predicted_root_cause="cartservice",
            top1_correct=True,
            top3_correct=True,
            rank=1,
            reciprocal_rank=1.0,
        ),
        _row(
            predicted_root_cause="front-end|cartservice",
            top1_correct=False,
            top3_correct=True,
            rank=2,
            reciprocal_rank=0.5,
        ),
        _row(
            predicted_root_cause="a|b|c|d|cartservice",
            top1_correct=False,
            top3_correct=False,
            rank=5,
            reciprocal_rank=0.2,
        ),
        _row(
            predicted_root_cause="cartservice",
            top1_correct=True,
            top3_correct=True,
            rank=1,
            reciprocal_rank=1.0,
        ),
    ]
    aggregates = score(rows)
    assert len(aggregates) == 1
    agg = aggregates[0]
    assert agg.method == "infra"
    assert agg.count == 4
    assert agg.top1 == pytest.approx(0.5)
    assert agg.top3 == pytest.approx(0.75)
    assert agg.mrr == pytest.approx((1.0 + 0.5 + 0.2 + 1.0) / 4)
    assert agg.median_latency_s == pytest.approx(30.0)
    assert agg.detected_rate == pytest.approx(1.0)
    assert agg.false_positive_rate == pytest.approx(0.0)


def test_score_median_latency_is_none_when_undetected() -> None:
    """Every run undetected -> median latency is None, not 0.0."""
    rows = [_row(detected=False, detection_latency_s=None, predicted_root_cause=None)]
    agg = score(rows)[0]
    assert agg.median_latency_s is None


def test_score_groups_by_set_and_method() -> None:
    """Two methods in one set produce two aggregates, in first-appearance order."""
    rows = [
        _row(method="infra"),
        _row(method="raw_alertmanager", method_kind="baseline", predicted_root_cause="cartservice"),
        _row(method="infra"),
    ]
    aggregates = score(rows)
    assert [a.method for a in aggregates] == ["infra", "raw_alertmanager"]
    assert aggregates[1].method_kind == "baseline"


def test_score_empty_returns_empty() -> None:
    """No rows -> no aggregates (not a zero row, which would look like a result)."""
    assert score([]) == []


def test_score_false_positive_rate() -> None:
    """The false-positive rate is the fraction of rows marked `false_positive`."""
    rows = [
        _row(false_positive=True),
        _row(false_positive=False),
        _row(false_positive=False),
        _row(false_positive=True),
    ]
    assert score(rows)[0].false_positive_rate == pytest.approx(0.5)


def test_default_scorer_derives_correctness_from_the_ranking() -> None:
    """`default_scorer` is the seam that turns a ranked list into a scored row."""
    spec = _spec(expected="cartservice")
    row = default_scorer(
        spec,
        1001,
        0,
        method="infra",
        method_kind="infra",
        run_id="dev-x@1001r0:infra",
        detected=True,
        ranked=["front-end", "cartservice"],
        detection_latency_s=42.0,
        false_positive=False,
        blocked=False,
    )
    assert row.top1_correct is False
    assert row.top3_correct is True
    assert row.rank == 2
    assert row.reciprocal_rank == pytest.approx(0.5)
    assert row.scenario_set == "dev"
    assert row.expected_root_cause == "cartservice"


def test_default_scorer_marks_blocked_rows() -> None:
    """A blocked scenario produces a row flagged `blocked`, not a fabricated accuracy."""
    spec = _spec()
    row = default_scorer(
        spec,
        1004,
        0,
        method="infra",
        method_kind="infra",
        run_id="dev-x@1004r0:infra",
        detected=False,
        ranked=[],
        detection_latency_s=None,
        false_positive=False,
        blocked=True,
    )
    assert row.blocked is True
    assert row.top1_correct is False
    assert row.reciprocal_rank == 0.0
