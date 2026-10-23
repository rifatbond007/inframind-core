"""Replay path tests: the deterministic, no-cluster path is the gate that must run green."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from inframind.evaluation.config import EvalParams
from inframind.evaluation.runner.replay import (
    ReplayError,
    compute_fault_window,
    load_graph_snapshot,
    make_run_id,
    replay_signals,
)
from inframind.evaluation.scenarios import fixture_dir_for, load_set


def _repo_fixtures(repo_paths: tuple[Path, Path]) -> Path:
    """The committed seed corpus lives at `evaluation/fixtures/<scenario_id>/`."""
    _scenarios, fixtures = repo_paths
    return fixtures


def test_replay_signals_reads_real_corpus(repo_paths: tuple[Path, Path]) -> None:
    """The committed seed corpus parses and is ts-ordered."""
    fixtures = _repo_fixtures(repo_paths)
    for path in fixtures.glob("*/signals.ndjson"):
        signals = replay_signals(path)
        assert signals, f"{path} has no signals"
        for prev, curr in itertools.pairwise(signals):
            assert curr.ts >= prev.ts


def test_replay_signals_rejects_non_monotone_ts(tmp_path: Path) -> None:
    """Out-of-order timestamps are a `ReplayError`, not a silent skip."""
    bad = tmp_path / "signals.ndjson"
    bad.write_text(
        """{"id":"01HFX000000000000000000001","ts":"2026-10-21T13:00:05+00:00","source":"loki","type":"log","service":"a","severity":"info","trace_id":null,"attrs":{}}
{"id":"01HFX000000000000000000002","ts":"2026-10-21T13:00:00+00:00","source":"loki","type":"log","service":"a","severity":"info","trace_id":null,"attrs":{}}
"""
    )
    with pytest.raises(ReplayError, match="ts not monotone"):
        replay_signals(bad)


def test_replay_signals_rejects_invalid_signal(tmp_path: Path) -> None:
    """A line that fails `Signal.model_validate` raises `ReplayError`."""
    bad = tmp_path / "signals.ndjson"
    bad.write_text('{"ts":"not-a-datetime","source":"loki","type":"log","service":"a"}\n')
    with pytest.raises(ReplayError, match="invalid signal"):
        replay_signals(bad)


def test_load_graph_snapshot_round_trips(repo_paths: tuple[Path, Path]) -> None:
    """A committed graph.json survives a load-snapshot round trip."""
    fixtures = _repo_fixtures(repo_paths)
    for path in fixtures.glob("*/graph.json"):
        graph = load_graph_snapshot(path)
        services = graph.services()
        assert services, f"{path}: empty graph after load"
        # Round-trip: snapshot back to dict and confirm we get the same services back.
        snap = graph.snapshot()
        snap_services = sorted(
            {node for edge in snap["edges"] for node in (edge["caller"], edge["callee"])}
        )
        assert sorted(services) == snap_services


def test_load_graph_snapshot_handles_missing() -> None:
    """Missing snapshot falls back to a cold-start empty graph rather than raising."""
    from inframind.rca.graph import ServiceGraph

    graph = load_graph_snapshot(Path("/nonexistent/graph.json"))
    assert isinstance(graph, ServiceGraph)
    assert graph.services() == []


def test_compute_fault_window_uses_onset_and_duration() -> None:
    """The fault window is `(onset, onset + duration_s)`."""
    from datetime import UTC, datetime

    onset = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    start, end = compute_fault_window(onset, duration_s=120.0)
    assert start == onset
    assert (end - start).total_seconds() == 120.0


def test_make_run_id_provenance() -> None:
    """`make_run_id` derives a deterministic provenance string from the inputs."""
    from inframind.evaluation.schema import ChaosSpec, ScenarioSpec

    spec = ScenarioSpec(
        scenario_id="dev-x",
        set="dev",
        seed=10,
        fault_type="pod_kill",
        target_service="svc",
        target_namespace="ns",
        duration_s=60.0,
        warmup_s=10.0,
        cooldown_s=10.0,
        expected_root_cause="svc",
        expected_affected_services=("svc",),
        chaos=ChaosSpec(),
        metrics_to_assert=("http_requests_total",),
        repeats=5,
    )
    assert make_run_id(spec=spec, seed=10, repeat=3, method="infra") == "dev-x@10r3:infra"
    assert (
        make_run_id(spec=spec, seed=10, repeat=3, method="raw_alertmanager")
        == "dev-x@10r3:raw_alertmanager"
    )


def test_full_replay_round_trip_for_real_fixtures(
    repo_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    """The committed fixtures replay end to end and produce a detected row."""
    from inframind.evaluation.runner.replay import replay_run
    from inframind.evaluation.scoring import default_scorer

    fixtures = _repo_fixtures(repo_paths)
    params = EvalParams(fixture_dir=str(fixtures))
    committed = {d.name for d in fixtures.iterdir() if d.is_dir()}
    scenarios = [s for s in load_set("dev") if s.scenario_id in committed]
    assert scenarios, "no committed fixture had a matching scenario"

    for spec in scenarios:
        fixture_dir = fixture_dir_for(spec, params)
        assert fixture_dir.exists(), f"missing fixture dir for {spec.scenario_id}"
        row = replay_run(
            fixture_dir,
            spec=spec,
            scorer=default_scorer,
            method="infra",
            method_kind="infra",
            run_id=make_run_id(spec=spec, seed=spec.seed, repeat=0, method="infra"),
            seed=spec.seed,
            repeat=0,
            params=params,
        )
        assert row.detected, f"{spec.scenario_id}: no incident opened"
        assert row.top1_correct, (
            f"{spec.scenario_id}: top-1 miss; ranked {row.predicted_root_cause!r}"
        )


def test_replay_run_is_repeatable(repo_paths: tuple[Path, Path], tmp_path: Path) -> None:
    """Same fixture twice produces the identical row (D6 determinism)."""
    from inframind.evaluation.runner.replay import replay_run
    from inframind.evaluation.scoring import default_scorer

    fixtures = _repo_fixtures(repo_paths)
    params = EvalParams(fixture_dir=str(fixtures))
    spec = next(
        s for s in load_set("dev") if s.scenario_id == "dev-podchaos-01-cartservice-pod-kill"
    )
    fixture_dir = fixture_dir_for(spec, params)
    row_a = replay_run(
        fixture_dir,
        spec=spec,
        scorer=default_scorer,
        method="infra",
        method_kind="infra",
        run_id=make_run_id(spec=spec, seed=spec.seed, repeat=0, method="infra"),
        seed=spec.seed,
        repeat=0,
        params=params,
    )
    row_b = replay_run(
        fixture_dir,
        spec=spec,
        scorer=default_scorer,
        method="infra",
        method_kind="infra",
        run_id=make_run_id(spec=spec, seed=spec.seed, repeat=0, method="infra"),
        seed=spec.seed,
        repeat=0,
        params=params,
    )
    assert row_a.predicted_root_cause == row_b.predicted_root_cause
    assert row_a.top1_correct == row_b.top1_correct


def test_replay_run_rejects_missing_fixture(
    repo_paths: tuple[Path, Path],
) -> None:
    """A missing `signals.ndjson` is a `ReplayError`, not an empty row.

    `replay_run` reads the fixture itself, so it must not paper over a missing corpus: a
    scenario that silently scored zero is the exact failure mode the loader refuses.
    """
    from inframind.evaluation.runner.replay import replay_run
    from inframind.evaluation.scoring import default_scorer

    fixtures = _repo_fixtures(repo_paths)
    params = EvalParams(fixture_dir=str(fixtures))
    spec = load_set("dev")[0]
    ghost_dir = fixture_dir_for(spec, params)  # a real scenario with no fixture committed

    if ghost_dir.exists():
        pytest.skip("every dev scenario now has a committed fixture")

    with pytest.raises(ReplayError, match="does not exist"):
        replay_run(
            ghost_dir,
            spec=spec,
            scorer=default_scorer,
            method="infra",
            method_kind="infra",
            run_id=make_run_id(spec=spec, seed=spec.seed, repeat=0, method="infra"),
            seed=spec.seed,
            repeat=0,
            params=params,
        )


def test_d1_invariant_no_llm_in_ranking_path(
    repo_paths: tuple[Path, Path],
) -> None:
    """The ranking path (`replay_run`) does not consult the LLM.

    Passing `on_llm_summary=None` exercises the default path; if a future change routes
    `summary` into `rank_candidates`, this test will catch it because the ranking will
    have changed.
    """
    from inframind.evaluation.runner.replay import replay_run
    from inframind.evaluation.scoring import default_scorer

    fixtures = _repo_fixtures(repo_paths)
    params = EvalParams(fixture_dir=str(fixtures))
    spec = next(
        s for s in load_set("dev") if s.scenario_id == "dev-podchaos-01-cartservice-pod-kill"
    )
    fixture_dir = fixture_dir_for(spec, params)
    no_llm_row = replay_run(
        fixture_dir,
        spec=spec,
        scorer=default_scorer,
        method="no-llm",
        method_kind="infra",
        run_id=make_run_id(spec=spec, seed=spec.seed, repeat=0, method="no-llm"),
        seed=spec.seed,
        repeat=0,
        params=params,
        on_llm_summary=None,
    )
    assert no_llm_row.predicted_root_cause == "cartservice"
