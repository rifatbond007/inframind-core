"""RCA parameters and their `INFRAMIND_RCA_<PARAM>` overrides.

Defaults come from `docs/standards/06-rca-scoring.md` and `13-service-graph-contract.md` (D21).
Tune on the dev split only (D6). `INFRAMIND_RCA_PAGERANK_ALPHA` is the MicroRCA-style restart
probability (standard 06.5). `INFRAMIND_RCA_TIEBREAK_SEED` makes tie-breaking reproducible
(standard 06.7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from inframind.common.config import load_prefixed


@dataclass(frozen=True)
class RCAParams:
    """Ranker weights (`w1`-`w4` sum to 1), walk depth, and graph tunables (D21)."""

    # Ranker weights (section 6.4); starting values, dev-split tuned.
    w_anomaly_severity: float = 0.45
    w_earliest_onset: float = 0.20
    w_downstream_depth: float = 0.25
    w_change_correlation: float = 0.10

    # Walk (section 6.3).
    max_depth: int = 4

    # Graph (section 13.4 / 13.5 / 13.6).
    ewma_alpha: float = 0.1
    min_observations: int = 3
    decay_half_life_s: float = 300.0
    snapshot_interval_s: float = 60.0
    max_samples: int = 100
    low_confidence_multiplier: float = 0.1

    # PageRank variant (section 6.5).
    pagerank_alpha: float = 0.85
    top_k: int = 5

    # Tie-break seed (section 6.7).
    tiebreak_seed: int = 0


def load_rca_params(env: Mapping[str, str] | None = None) -> RCAParams:
    """Build `RCAParams` from env (`os.environ` when `env` is None)."""
    return load_prefixed(RCAParams, "INFRAMIND_RCA", env)


def change_correlation_pre_window_s(env: Mapping[str, str] | None = None) -> float:
    """Return `CHANGE_CORRELATION_PRE_WINDOW_S` in seconds (default 1800 = 30 min)."""
    import os

    source = os.environ if env is None else env
    if "CHANGE_CORRELATION_PRE_WINDOW_S" not in source:
        return 1800.0
    try:
        return float(source["CHANGE_CORRELATION_PRE_WINDOW_S"])
    except ValueError as e:
        raise ValueError(
            f"CHANGE_CORRELATION_PRE_WINDOW_S={source['CHANGE_CORRELATION_PRE_WINDOW_S']!r} "
            f"is not a number of seconds"
        ) from e
