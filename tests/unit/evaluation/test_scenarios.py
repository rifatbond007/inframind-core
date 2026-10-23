"""Scenarios loader tests (D6, D2-Chaos-runtime)."""

from __future__ import annotations

from pathlib import Path

import pytest

from inframind.evaluation.scenarios import (
    BLOCKED_REASON,
    ScenarioError,
    assert_disjoint,
    blocked,
    fixture_dir_for,
    load_scenario_file,
    load_set,
    parse_scenario,
    runnable,
)


def test_parse_scenario_minimal_round_trip() -> None:
    """A complete, runnable scenario parses into a `ScenarioSpec`."""
    raw = {
        "scenario_id": "test-minimal",
        "set": "dev",
        "seed": 1,
        "fault_type": "pod_kill",
        "target_service": "svc",
        "target_namespace": "ns",
        "duration_s": 60.0,
        "warmup_s": 10.0,
        "cooldown_s": 10.0,
        "expected_root_cause": "svc",
        "expected_affected_services": ["svc"],
        "metrics_to_assert": ["http_requests_total"],
        "repeats": 5,
    }
    spec = parse_scenario(raw, source="inline")
    assert spec.scenario_id == "test-minimal"
    assert spec.set == "dev"
    assert spec.expected_root_cause == "svc"
    assert spec.is_blocked is False


def test_parse_scenario_blocked_requires_reason() -> None:
    """A blocked scenario without `block_reason` is a load error."""
    raw = {
        "scenario_id": "test-blocked",
        "set": "dev",
        "seed": 1,
        "fault_type": "cpu_stress",
        "target_service": "svc",
        "target_namespace": "ns",
        "duration_s": 60.0,
        "warmup_s": 10.0,
        "cooldown_s": 10.0,
        "expected_root_cause": "svc",
        "expected_affected_services": ["svc"],
        "metrics_to_assert": ["http_requests_total"],
        "repeats": 5,
        "status": "blocked",
    }
    with pytest.raises(ScenarioError, match="block_reason"):
        parse_scenario(raw, source="inline")


def test_parse_scenario_rejects_repeats_below_five() -> None:
    """D6: a scenario must request ≥5 repeats."""
    raw = {
        "scenario_id": "test-low-repeats",
        "set": "dev",
        "seed": 1,
        "fault_type": "pod_kill",
        "target_service": "svc",
        "target_namespace": "ns",
        "duration_s": 60.0,
        "warmup_s": 10.0,
        "cooldown_s": 10.0,
        "expected_root_cause": "svc",
        "expected_affected_services": ["svc"],
        "metrics_to_assert": ["http_requests_total"],
        "repeats": 3,
    }
    with pytest.raises(ScenarioError, match="repeats must be ≥5"):
        parse_scenario(raw, source="inline")


def test_load_set_rejects_wrong_set_declaration(repo_paths: tuple[Path, Path]) -> None:
    """A scenario file declaring `set: dev` living in `test/` is an error."""
    scenarios_root, _fixtures = repo_paths
    test_dir = scenarios_root / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    wrong = test_dir / "00-wrong-set.yaml"
    wrong.write_text(
        """scenario_id: test-wrong-set
set: dev
seed: 1
fault_type: pod_kill
target_service: svc
target_namespace: ns
duration_s: 60
warmup_s: 10
cooldown_s: 10
expected_root_cause: svc
expected_affected_services: [svc]
chaos: {kind: PodChaos, spec: {action: pod-kill, mode: one, selector: {label: app, value: svc}}}
metrics_to_assert: [http_requests_total]
repeats: 5
"""
    )
    try:
        with pytest.raises(ScenarioError, match="declares set"):
            load_set("test")
    finally:
        if wrong.exists():
            wrong.unlink()


def test_load_dev_and_test_sets_are_disjoint(repo_paths: tuple[Path, Path]) -> None:
    """D6: no `scenario_id` appears in both `dev/` and `test/`."""
    _scenarios_root, _fixtures = repo_paths
    dev = load_set("dev")
    test = load_set("test")
    overlap = {s.scenario_id for s in dev} & {s.scenario_id for s in test}
    assert not overlap, f"scenario ids in both dev and test: {sorted(overlap)}"


def test_dev_set_is_eight_scenarios(repo_paths: tuple[Path, Path]) -> None:
    """The dev plan locked 8 scenarios; assert the count and split."""
    _scenarios_root, _fixtures = repo_paths
    dev = load_set("dev")
    assert len(dev) == 8, f"expected 8 dev scenarios, got {len(dev)}"
    assert len(runnable(dev)) == 4, "expected 4 runnable dev scenarios"
    assert len(blocked(dev)) == 4, "expected 4 blocked dev scenarios"


def test_test_set_is_six_scenarios(repo_paths: tuple[Path, Path]) -> None:
    """The test plan locked 6 scenarios; assert the count and split."""
    _scenarios_root, _fixtures = repo_paths
    test = load_set("test")
    assert len(test) == 6, f"expected 6 test scenarios, got {len(test)}"
    assert len(runnable(test)) == 4
    assert len(blocked(test)) == 2


def test_blocked_scenarios_all_carry_d2_chaos_runtime_docker(repo_paths: tuple[Path, Path]) -> None:
    """Every blocked scenario carries the exact D2-Chaos-runtime reason."""
    _scenarios_root, _fixtures = repo_paths
    blocked_dev = blocked(load_set("dev"))
    blocked_test = blocked(load_set("test"))
    all_blocked = blocked_dev + blocked_test
    assert all(s.block_reason == BLOCKED_REASON for s in all_blocked)
    # The plan encoded exactly 6 blocked scenarios (4 dev + 2 test).
    assert len(all_blocked) == 6


def test_assert_disjoint_raises_on_overlap() -> None:
    """`assert_disjoint` raises when an id appears in both sets."""
    dev_a = parse_scenario(_minimal("dup"), source="a")
    test_a = parse_scenario({**_minimal("dup"), "set": "test"}, source="b")
    with pytest.raises(ScenarioError, match="both dev and test"):
        assert_disjoint([dev_a], [test_a])


def test_fixture_dir_for_uses_scenario_id() -> None:
    """The fixture directory name matches the scenario id."""
    spec = parse_scenario(_minimal("foo"), source="foo")
    from inframind.evaluation.config import EvalParams

    params = EvalParams()
    assert fixture_dir_for(spec, params).name == "foo"


def _minimal(scenario_id: str) -> dict:
    """Build a minimal valid scenario mapping for parser-only tests."""
    return {
        "scenario_id": scenario_id,
        "set": "dev",
        "seed": 1,
        "fault_type": "pod_kill",
        "target_service": "svc",
        "target_namespace": "ns",
        "duration_s": 60.0,
        "warmup_s": 10.0,
        "cooldown_s": 10.0,
        "expected_root_cause": "svc",
        "expected_affected_services": ["svc"],
        "metrics_to_assert": ["http_requests_total"],
        "repeats": 5,
    }


def test_load_scenario_file_reads_real_yaml(tmp_path: Path) -> None:
    """`load_scenario_file` reads a YAML file from disk and parses it."""
    yaml_path = tmp_path / "scenario.yaml"
    yaml_path.write_text(
        """scenario_id: tmp-scenario
set: dev
seed: 7
fault_type: pod_kill
target_service: svc
target_namespace: ns
duration_s: 60
warmup_s: 10
cooldown_s: 10
expected_root_cause: svc
expected_affected_services: [svc]
chaos:
  kind: PodChaos
  spec:
    action: pod-kill
    mode: one
    selector: {label: app, value: svc}
metrics_to_assert: [http_requests_total]
repeats: 5
"""
    )
    spec = load_scenario_file(yaml_path)
    assert spec.scenario_id == "tmp-scenario"
    assert spec.chaos.kind == "PodChaos"


def test_load_scenario_file_rejects_missing_keys(tmp_path: Path) -> None:
    """An incomplete YAML is a ScenarioError, never a silent skip."""
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("scenario_id: x\nset: dev\n")  # most fields missing
    with pytest.raises(ScenarioError, match="missing required key"):
        load_scenario_file(yaml_path)
