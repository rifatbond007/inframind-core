"""Evaluation tunables and their `INFRAMIND_EVAL_<PARAM>` overrides (standard 10.3).

Defaults come from P7 design: ≥5 repeats (D6), a dev/test split, an allow-blocked flag that
defaults to off so the harness refuses to silently score a blocked scenario as zero.

`INFRAMIND_EVAL_SEED`, when set, **overrides** the scenario's own seed. That is the route a
makes-the-run-bit-reproducible path takes — same seed in, same number out, forever.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from inframind.common.config import load_prefixed


@dataclass(frozen=True)
class EvalParams:
    """Tunables that drive one `make eval` invocation."""

    # Which scenario split to run. `dev` for tuning, `test` is scored once.
    scenario_set: str = "dev"

    # Number of repeats per scenario (D6: ≥5).
    repeats: int = 5

    # Path to the replay corpus and where results land. Strings rather than Paths because
    # pytest fixtures construct EvalParams from dicts without a working directory.
    fixture_dir: str = "evaluation/fixtures"
    results_dir: str = "evaluation/results/phase7"

    # How long after fault onset an incident must open by to count as "detected".
    detect_window_s: float = 120.0

    # Open-blocked scenarios anyway (used for fixture recording before live runs exist).
    allow_blocked: bool = False

    # Refuse to overwrite an existing results directory unless this is true.
    allow_overwrite: bool = False

    # The two scenario set names. `eval-all` runs them both as one invocation.
    sets: tuple[str, ...] = ("dev", "test")

    # Optional per-run seed override. None means "honour each scenario's seed".
    seed_override: int | None = None

    # Whether to run the ablations in `make eval-ablation`. default on.
    run_ablations: bool = True

    # Whether to run the four D7 baselines alongside InfraMind. default on.
    run_baselines: bool = True

    # Extra env keys we recognise but don't expose on the dataclass yet.
    extra: dict[str, str] = field(default_factory=dict)


def load_eval_params(env: Mapping[str, str] | None = None) -> EvalParams:
    """Build `EvalParams` from env (`os.environ` when `env` is None)."""
    base = load_prefixed(EvalParams, "INFRAMIND_EVAL", env)
    seed_raw = (env or os.environ).get("INFRAMIND_EVAL_SEED", "").strip()
    seed_override = int(seed_raw) if seed_raw else None
    # Replace seed_override in the frozen dataclass via a fresh construction (frozen
    # dataclasses cannot be assigned to); dataclasses.replace handles this cleanly.
    from dataclasses import replace

    return replace(base, seed_override=seed_override)


def eval_set_or_all(value: str | None) -> tuple[str, ...]:
    """Parse a `--set` flag into the tuple of sets that should run."""
    value = (value or "dev").strip().lower()
    if value in ("all", "*"):
        return ("dev", "test")
    if value in ("dev", "test"):
        return (value,)
    raise ValueError(f"unknown scenario set {value!r}; expected dev|test|all")


__all__ = ["EvalParams", "eval_set_or_all", "load_eval_params"]
