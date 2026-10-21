"""Replay and live runners for P7.

`replay` is the deterministic, no-cluster path — the gate that runs green on this dev box.
`live` is the cluster-touching path; it is the only module in `evaluation/` that calls
`kubectl`, and it refuses any context that does not start with `kind-`.
"""

from __future__ import annotations

from .replay import (
    ReplayError,
    ReplayInputs,
    ReplayResult,
    Scorer,
    make_run_id,
    replay_run,
    replay_signals,
)

__all__ = [
    "ReplayError",
    "ReplayInputs",
    "ReplayResult",
    "Scorer",
    "make_run_id",
    "replay_run",
    "replay_signals",
]
