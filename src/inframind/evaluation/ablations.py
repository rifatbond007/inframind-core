"""The four ablations (standard 10.8).

Each ablation is a one-line substitution into the same `replay_run`:

* `no-correlation` — detection per-anomaly (every anomaly opens its own incident). Set
  ``INFRAMIND_CORRELATION_ENABLED=false``.
* `no-rca` — graph-guided ranking removed; fallback to score order alone. Implemented as
  a flag on the ranker (we surface this through a `no-rca` *method* rather than a separate
  ranker file).
* `no-llm` — the D1 invariant (the LLM never decides anything) gets its own row. With
  `no-llm` the row must be byte-identical to the `infra` row by construction.
* `no-change-correlation` — the ``change_correlation`` term is dropped from the P5 score
  (sets ``INFRAMIND_RCA_W_CHANGE_CORRELATION=0``).

`no-llm` is the interesting one: it exists to catch a code path that leaks model output
into ranking. The harness asserts the invariant on a fixture.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from inframind.common.anomaly import Anomaly
from inframind.correlation.config import CorrelationParams
from inframind.rca.config import RCAParams
from inframind.rca.graph import ServiceGraph
from inframind.rca.rank import rank_candidates

#: The four ablation names, in report order.
ABLATION_NAMES: tuple[str, ...] = (
    "no-correlation",
    "no-rca",
    "no-llm",
    "no-change-correlation",
)


def params_for(name: str, *, rca: RCAParams) -> RCAParams:
    """Return the `RCAParams` for ablation `name`, with one field disabled.

    `no-llm` does not change `RCAParams`; the invariant is that `rank_candidates` is
    never called with model output. `no-rca` is handled outside the ranker (we keep the
    inputs but skip the call), so `params_for(no-rca)` returns the unmodified params.
    """
    if name in ("no-llm", "no-rca"):
        return rca
    if name == "no-correlation":
        # Surface as a flag the caller reads; we return the unmodified params here.
        return rca
    if name == "no-change-correlation":
        return dataclasses.replace(rca, w_change_correlation=0.0)
    raise ValueError(f"unknown ablation {name!r}; expected one of {ABLATION_NAMES}")


def correlation_params_for(name: str, *, base: CorrelationParams) -> CorrelationParams:
    """Return the `CorrelationParams` for ablation `name`."""
    if name != "no-correlation":
        return base
    return dataclasses.replace(base, enabled=False)


def rank_with_ablation(
    name: str,
    *,
    incident_id: str,
    symptoms: Sequence[str],
    anomalies: Sequence[Anomaly],
    graph: ServiceGraph,
    window: tuple,
    rca: RCAParams,
) -> list[Any]:
    """Rank with ablation `name` applied. Returns a list of `Candidate`.

    `no-rca` falls back to the score-order on the per-service anomaly set; we use the
    `rank_candidates` function rather than re-implementing here so the offset between
    the two paths is just one flag.
    """
    params = params_for(name, rca=rca)
    if name == "no-rca":
        # Strip the graph term by zeroing the structural weight. Depth + onset still apply.
        params = dataclasses.replace(params, w_downstream_depth=0.0, low_confidence_multiplier=1.0)
    return rank_candidates(
        incident_id=incident_id,
        symptoms=symptoms,
        anomalies=anomalies,
        graph=graph,
        window=window,
        params=params,
    )


def is_no_llm(name: str) -> bool:
    """True when `name` is the `no-llm` ablation."""
    return name == "no-llm"


__all__ = [
    "ABLATION_NAMES",
    "correlation_params_for",
    "is_no_llm",
    "params_for",
    "rank_with_ablation",
]
