"""Evaluation harness (P7, standard 10).

Measures InfraMind against the six locked D7 baselines, reproducibly, from
`(scenario_id, seed)` (D6). Two paths:

* **replay** — feed an NDJSON fixture through detection → correlation → ranking. No
  Kubernetes; deterministic; the gate that runs green on a dev box with no docker daemon.
* **live** — apply Chaos Mesh against a `kind-*` cluster. Ships unexecuted; the guards
  (kind-only context, blocked-scenario refusal) are wired so the first real run inherits
  them.

The DAG permits `evaluation` to import `common`, `correlation`, `detection`, `rca`, and
`llm` — **not** `alerting`, `storage`, or `api`. The harness measures the pipeline; the P6
storage and HTTP path is measured by its own API integration tests.
"""

from __future__ import annotations

from .ablations import ABLATION_NAMES
from .baselines import BASELINE_NAMES, Baseline, BaselineInput
from .config import EvalParams, eval_set_or_all, load_eval_params
from .report import write_all
from .scenarios import ScenarioError, assert_disjoint, blocked, load_set, runnable
from .schema import (
    ChaosSpec,
    FixtureMeta,
    MethodKind,
    ResultRow,
    ScenarioSet,
    ScenarioSpec,
)
from .scoring import MethodAggregate, score, score_row

__all__ = [
    "ABLATION_NAMES",
    "BASELINE_NAMES",
    "Baseline",
    "BaselineInput",
    "ChaosSpec",
    "EvalParams",
    "FixtureMeta",
    "MethodAggregate",
    "MethodKind",
    "ResultRow",
    "ScenarioError",
    "ScenarioSet",
    "ScenarioSpec",
    "assert_disjoint",
    "blocked",
    "eval_set_or_all",
    "load_eval_params",
    "load_set",
    "runnable",
    "score",
    "score_row",
    "write_all",
]
