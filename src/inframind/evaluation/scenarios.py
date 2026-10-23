"""Scenario loading: one YAML per fault, validated into a `ScenarioSpec` (standard 10.4).

Two invariants the loader exists to enforce:

* **A malformed scenario is a load error, not a skip.** A scenario you meant to run that
  never loaded is worse than a crash — a silent skip turns "12 scenarios" into a smaller
  denominator without anyone noticing.
* **The dev/test split is sacred** (D6). `load_set("dev")` refuses a file declaring
  `set: test` and vice versa. The split is not a convention; it is a check.
* **`chaos` is optional.** The one test scenario that changes a ConfigMap injects no chaos
  at all; forcing an empty `chaos:` block on it would be noise, not clarity.

Blocked scenarios (D2-Chaos-runtime) load normally and carry `status: blocked`. The runner
refuses to score them unless `allow_blocked`, and when allowed it writes rows marked
`blocked: true` — a fabricated accuracy would be worse than an honest gap.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml

from inframind.common.exceptions import InfraMindError
from inframind.common.logging import get_logger

from .config import EvalParams
from .schema import ChaosSpec, ScenarioSet, ScenarioSpec

log = get_logger(__name__)

#: Repository root, so scenario paths resolve the same way regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCENARIO_ROOT = REPO_ROOT / "evaluation" / "scenarios"

#: The block reason pinned for the 4 scenarios D2-Chaos-runtime makes unrunnable.
BLOCKED_REASON = "d2_chaos_runtime_docker"

_REQUIRED = (
    "scenario_id",
    "set",
    "seed",
    "fault_type",
    "target_service",
    "target_namespace",
    "duration_s",
    "warmup_s",
    "cooldown_s",
    "expected_root_cause",
    "expected_affected_services",
    "metrics_to_assert",
    "repeats",
)


class ScenarioError(InfraMindError):
    """A scenario file is missing, malformed, or violates the dev/test split."""


def parse_scenario(raw: dict[str, Any], *, source: str) -> ScenarioSpec:
    """Validate one decoded YAML mapping into a `ScenarioSpec`.

    Args:
        raw: the decoded YAML.
        source: the file name, used in error messages so a bad file is identifiable.

    Raises:
        ScenarioError: a required key is missing, a value has the wrong type, or the
            scenario declares a `set` inconsistent with the directory it was loaded from.
    """
    missing = [key for key in _REQUIRED if key not in raw]
    if missing:
        raise ScenarioError(f"{source}: missing required key(s) {missing}")

    set_value = raw["set"]
    if set_value not in ("dev", "test"):
        raise ScenarioError(f"{source}: set must be 'dev' or 'test', got {set_value!r}")

    chaos_raw = raw.get("chaos") or {}
    status = raw.get("status", "runnable")
    if status not in ("runnable", "blocked"):
        raise ScenarioError(f"{source}: status must be 'runnable' or 'blocked', got {status!r}")

    spec_inner = chaos_raw.get("spec") or {}
    selector = spec_inner.get("selector") or {}

    try:
        spec = ScenarioSpec(
            scenario_id=str(raw["scenario_id"]),
            set=set_value,
            seed=int(raw["seed"]),
            fault_type=str(raw["fault_type"]),
            target_service=str(raw["target_service"]),
            target_namespace=str(raw["target_namespace"]),
            duration_s=float(raw["duration_s"]),
            warmup_s=float(raw["warmup_s"]),
            cooldown_s=float(raw["cooldown_s"]),
            expected_root_cause=str(raw["expected_root_cause"]),
            expected_affected_services=tuple(raw["expected_affected_services"]),
            chaos=ChaosSpec(
                kind=str(chaos_raw.get("kind", "")),
                action=str(spec_inner.get("action", "")),
                mode=str(spec_inner.get("mode", "")),
                selector={str(k): str(v) for k, v in selector.items()},
            ),
            metrics_to_assert=tuple(raw["metrics_to_assert"]),
            repeats=int(raw["repeats"]),
            status=status,
            block_reason=str(raw.get("block_reason", "")),
        )
    except (TypeError, ValueError) as e:
        raise ScenarioError(f"{source}: field type error: {e}") from e

    if spec.repeats < 5:
        raise ScenarioError(f"{source}: repeats must be ≥5 per D6, got {spec.repeats}")
    if spec.is_blocked and not spec.block_reason:
        raise ScenarioError(f"{source}: a blocked scenario must declare block_reason")
    return spec


def load_scenario_file(path: Path) -> ScenarioSpec:
    """Parse one scenario YAML file. Raises `ScenarioError` on malformed input."""
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ScenarioError(f"{path}: unreadable: {e}") from e
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path}: top-level YAML must be a mapping, got {type(raw).__name__}")
    return parse_scenario(raw, source=path.name)


def load_set(
    scenario_set: ScenarioSet | str,
    *,
    root: Path | None = None,
) -> list[ScenarioSpec]:
    """Load every scenario in `root/<scenario_set>/`, sorted by id.

    Raises:
        ScenarioError: a file declares the wrong `set`, two scenarios share an id, or any
            file is malformed.
    """
    scenario_set = scenario_set  # narrow the type for the index below
    if scenario_set not in ("dev", "test"):
        raise ScenarioError(f"unknown scenario set {scenario_set!r}; expected dev|test")

    base = root or DEFAULT_SCENARIO_ROOT
    directory = base / scenario_set
    if not directory.exists():
        raise ScenarioError(f"{directory} does not exist")

    specs: list[ScenarioSpec] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        spec = load_scenario_file(path)
        if spec.set != scenario_set:
            raise ScenarioError(
                f"{path.name}: declares set={spec.set!r} but lives in {scenario_set}/"
            )
        if spec.scenario_id in seen:
            raise ScenarioError(f"{path.name}: duplicate scenario_id {spec.scenario_id!r}")
        seen.add(spec.scenario_id)
        specs.append(spec)

    return specs


def runnable(specs: Iterable[ScenarioSpec]) -> list[ScenarioSpec]:
    """Filter to scenarios that are not blocked by D2-Chaos-runtime."""
    return [spec for spec in specs if not spec.is_blocked]


def blocked(specs: Iterable[ScenarioSpec]) -> list[ScenarioSpec]:
    """Filter to scenarios blocked by D2-Chaos-runtime."""
    return [spec for spec in specs if spec.is_blocked]


def assert_disjoint(dev: Sequence[ScenarioSpec], test: Sequence[ScenarioSpec]) -> None:
    """Raise when any `scenario_id` appears in both splits (D6)."""
    dev_ids = {s.scenario_id for s in dev}
    test_ids = {s.scenario_id for s in test}
    overlap = dev_ids & test_ids
    if overlap:
        raise ScenarioError(f"scenario_id(s) appear in both dev and test: {sorted(overlap)}")


def fixture_dir_for(spec: ScenarioSpec, params: EvalParams) -> Path:
    """Return the fixture directory for `spec` under `params.fixture_dir`."""
    return Path(params.fixture_dir) / spec.fixture_dirname


def _debug_dump(spec: ScenarioSpec) -> str:
    """Return a JSON line for the spec; used by `scripts/` debugging, not the pipeline."""
    return json.dumps(
        {
            "scenario_id": spec.scenario_id,
            "set": spec.set,
            "fault_type": spec.fault_type,
            "status": spec.status,
        },
        sort_keys=True,
    )


__all__ = [
    "BLOCKED_REASON",
    "DEFAULT_SCENARIO_ROOT",
    "ScenarioError",
    "assert_disjoint",
    "blocked",
    "fixture_dir_for",
    "load_scenario_file",
    "load_set",
    "parse_scenario",
    "runnable",
]
