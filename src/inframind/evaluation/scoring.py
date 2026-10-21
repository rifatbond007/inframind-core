"""Scoring (standard 10.7).

Pure functions that turn a list of `ResultRow` into the four aggregate numbers the paper
uses: top-1, top-3, MRR, median detection latency. Plus the false-positive rate from the
fault-free window.

Two contracts:

* No I/O. Scorers read rows and return aggregates.
* Aggregates expose the same field names P9 renders: `top1`, `top3`, `mrr`, `latency_s`,
  `false_positive`, `detection`, `count`. A change here is a paper-bug because P9's LaTeX
  table reads these names verbatim.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass

from .schema import MethodKind, ResultRow


@dataclass(frozen=True, slots=True)
class MethodAggregate:
    """Aggregated score for one (scenario set, method) pair."""

    method: str
    method_kind: MethodKind
    scenario_set: str
    count: int
    detected_rate: float
    top1: float
    top3: float
    mrr: float
    median_latency_s: float | None
    false_positive_rate: float

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dict. NaN/None are kept verbatim for the paper writer."""
        return asdict(self)


def _safe_median(values: Iterable[float]) -> float | None:
    """Return the median, or `None` for an empty input. NaN allowed to pass through."""
    items = list(values)
    if not items:
        return None
    return statistics.median(items)


def score(rows: Sequence[ResultRow]) -> list[MethodAggregate]:
    """Group rows by (scenario_set, method, method_kind) and aggregate them.

    Method order is the order of first appearance, so callers can use this to drive a
    per-set, per-method report.
    """
    if not rows:
        return []

    grouped: dict[tuple[str, str, str], list[ResultRow]] = {}
    order: list[tuple[str, str, str]] = []
    method_kind_of: dict[str, MethodKind] = {
        "infra": "infra",
        "baseline": "baseline",
        "ablation": "ablation",
    }
    for row in rows:
        key = (row.scenario_set, row.method, row.method_kind)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    aggregates: list[MethodAggregate] = []
    for group_key in order:
        group = grouped[group_key]
        set_name, method, kind = group_key
        kind_value: MethodKind = method_kind_of.get(kind, "baseline")
        detected_count = sum(1 for r in group if r.detected)
        top1_count = sum(1 for r in group if r.top1_correct)
        top3_count = sum(1 for r in group if r.top3_correct)
        mr = sum(r.reciprocal_rank for r in group)
        latencies = [r.detection_latency_s for r in group if r.detection_latency_s is not None]
        fp_count = sum(1 for r in group if r.false_positive)
        n = len(group)
        aggregates.append(
            MethodAggregate(
                method=method,
                method_kind=kind_value,
                scenario_set=set_name,
                count=n,
                detected_rate=detected_count / n if n else 0.0,
                top1=top1_count / n if n else 0.0,
                top3=top3_count / n if n else 0.0,
                mrr=mr / n if n else 0.0,
                median_latency_s=_safe_median(latencies),
                false_positive_rate=fp_count / n if n else 0.0,
            )
        )
    return aggregates


def score_row(row: ResultRow) -> ResultRow:
    """Return the row with `top1_correct` / `top3_correct` / `reciprocal_rank` set.

    Used by both the InfraMind replay and the baselines: a row's correctness terms are
    derived from the ranked candidate list and the expected root cause. They live on the
    row so a CSV write is one-shot and the verification test stays tiny.

    `top3_correct` and `reciprocal_rank` follow standard 10.7 definitions. `predicted_root_cause`
    is the service at rank 1, or empty when nothing was detected.
    """
    if not row.detected or not row.method:
        # Detection failures: top1 / top3 / reciprocal_rank are 0, rank is 0. Pass the row
        # through with a normalisation that an empty ranking produces the canonical "miss".
        return _normalize(row, top1=False, top3=False, reciprocal_rank=0.0, rank=0, predicted="")

    ranked = list(row.predicted_root_cause.split("|")) if row.predicted_root_cause else []
    if not ranked:
        return _normalize(row, top1=False, top3=False, reciprocal_rank=0.0, rank=0, predicted="")

    expected = row.expected_root_cause
    rank_one = ranked[0] == expected
    rank_three_match = expected in ranked[:3]
    position = ranked.index(expected) if expected in ranked else -1
    reciprocal = 1.0 / (position + 1) if position >= 0 else 0.0
    return _normalize(
        row,
        top1=rank_one,
        top3=rank_three_match,
        reciprocal_rank=reciprocal,
        rank=position + 1 if position >= 0 else 0,
        predicted=ranked[0],
    )


def _normalize(
    row: ResultRow,
    *,
    top1: bool,
    top3: bool,
    reciprocal_rank: float,
    rank: int,
    predicted: str,
) -> ResultRow:
    """Copy a row with the scoring fields replaced. Frozen dataclass => `dataclasses.replace`."""
    from dataclasses import replace

    if not math.isfinite(reciprocal_rank):
        reciprocal_rank = 0.0
    return replace(
        row,
        top1_correct=top1,
        top3_correct=top3,
        reciprocal_rank=reciprocal_rank,
        rank=rank,
        predicted_root_cause=predicted or None,
    )


def default_scorer(
    spec,
    seed: int,
    repeat: int,
    *,
    method: str,
    method_kind: str,
    run_id: str,
    detected: bool,
    ranked: Sequence[str],
    detection_latency_s: float | None,
    false_positive: bool,
    blocked: bool,
) -> ResultRow:
    """The default `scorer` implementation used by `replay_run` for the infra variant.

    Defined here (not in `runner.replay`) so the dependency order stays one-way: scoring
    is a leaf, the runner calls it.
    """
    from .schema import ResultRow as _ResultRow

    predicted = "|".join(ranked) if ranked else ""
    row = _ResultRow(
        scenario_id=spec.scenario_id,
        seed=seed,
        repeat=repeat,
        method=method,
        method_kind=method_kind,  # type: ignore[arg-type]
        detected=detected,
        top1_correct=False,
        top3_correct=False,
        reciprocal_rank=0.0,
        rank=0,
        detection_latency_s=detection_latency_s,
        false_positive=false_positive,
        scenario_set=spec.set,
        run_id=run_id,
        expected_root_cause=spec.expected_root_cause,
        predicted_root_cause=predicted or None,
        blocked=blocked,
    )
    return score_row(row)


__all__ = ["MethodAggregate", "default_scorer", "score", "score_row"]
