"""Full replay integration: the seed corpus end to end, with no cluster.

This is the gate that runs green on a dev box with no docker daemon. Unlike P6's Postgres
integration tests, nothing here skips on a missing backend — the whole point of replay is
that the pipeline is a pure function of a fixture (D6).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inframind.evaluation.config import EvalParams
from inframind.evaluation.report import write_all
from inframind.evaluation.runner.main import _run_baselines, main
from inframind.evaluation.runner.replay import make_run_id, replay_run
from inframind.evaluation.scenarios import blocked, load_set, runnable
from inframind.evaluation.scoring import default_scorer, score

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "evaluation" / "fixtures"


def _runnable_dev_with_fixtures(params: EvalParams) -> list:
    """The dev scenarios that are runnable AND have a committed fixture."""
    committed = {d.name for d in FIXTURES.iterdir() if d.is_dir()}
    return [s for s in runnable(load_set("dev")) if s.scenario_id in committed]


def test_replay_corpus_reproduces_top1() -> None:
    """Every committed fixture replays to a top-1 hit. This is the harness working."""
    params = EvalParams(fixture_dir=str(FIXTURES))
    scenarios = _runnable_dev_with_fixtures(params)
    assert scenarios, "no committed fixture for a runnable dev scenario"

    for spec in scenarios:
        row = replay_run(
            FIXTURES / spec.scenario_id,
            spec=spec,
            scorer=default_scorer,
            method="infra",
            method_kind="infra",
            run_id=make_run_id(spec=spec, seed=spec.seed, repeat=0, method="infra"),
            seed=spec.seed,
            repeat=0,
            params=params,
        )
        assert row.detected, f"{spec.scenario_id}: nothing detected"
        assert row.top1_correct, (
            f"{spec.scenario_id}: top-1 miss "
            f"(expected {spec.expected_root_cause!r}, ranked {row.predicted_root_cause!r})"
        )


def test_replay_is_deterministic_across_repeats() -> None:
    """Replaying the same fixture five times gives the same answer every time (D6)."""
    params = EvalParams(fixture_dir=str(FIXTURES))
    spec = _runnable_dev_with_fixtures(params)[0]

    rankings = set()
    for repeat in range(params.repeats):
        row = replay_run(
            FIXTURES / spec.scenario_id,
            spec=spec,
            scorer=default_scorer,
            method="infra",
            method_kind="infra",
            run_id=make_run_id(spec=spec, seed=spec.seed, repeat=repeat, method="infra"),
            seed=spec.seed,
            repeat=repeat,
            params=params,
        )
        rankings.add(row.predicted_root_cause)

    assert len(rankings) == 1, f"replay was not deterministic: {rankings}"


def test_seed_override_makes_a_run_bit_reproducible() -> None:
    """`INFRAMIND_EVAL_SEED` overrides the scenario seed and is recorded in the row."""
    params = EvalParams(fixture_dir=str(FIXTURES), seed_override=99999)
    spec = _runnable_dev_with_fixtures(params)[0]
    row = replay_run(
        FIXTURES / spec.scenario_id,
        spec=spec,
        scorer=default_scorer,
        method="infra",
        method_kind="infra",
        run_id=make_run_id(spec=spec, seed=99999, repeat=0, method="infra"),
        seed=99999,
        repeat=0,
        params=params,
    )
    assert row.seed == 99999
    assert "@99999" in row.run_id


def test_every_baseline_produces_a_ranking_for_every_fixture() -> None:
    """All six D7 baselines run over the corpus without raising."""
    from inframind.evaluation.runner.main import _baseline_input_from_replay
    from inframind.evaluation.runner.replay import replay_signals

    params = EvalParams(fixture_dir=str(FIXTURES))
    for spec in _runnable_dev_with_fixtures(params):
        signals = replay_signals(FIXTURES / spec.scenario_id / "signals.ndjson")
        baseline_input = _baseline_input_from_replay(signals=signals, incidents=[])
        rows = _run_baselines(
            spec=spec,
            seed=spec.seed,
            repeat=0,
            baseline_input=baseline_input,
            pagerank_services=["cartservice"],
            summary_text="cartservice looks like the culprit",
        )
        assert len(rows) == 6, f"{spec.scenario_id}: expected 6 baseline rows, got {len(rows)}"
        for row in rows:
            assert row.method_kind == "baseline"
            assert row.run_id


def test_the_full_report_writes_a_complete_summary(tmp_path: Path) -> None:
    """A full pass over the corpus writes CSVs, summary.json, and the README."""
    from inframind.evaluation.runner.main import _run_one

    params = EvalParams(fixture_dir=str(FIXTURES))
    rows = []
    for spec in _runnable_dev_with_fixtures(params):
        for repeat in range(params.repeats):
            rows.extend(
                _run_one(
                    spec=spec,
                    params=params,
                    repeat=repeat,
                    run_baselines=True,
                    run_ablations=False,
                )
            )

    assert rows, "no rows produced by a full pass"
    aggregates = score(rows)
    assert aggregates
    infra = [a for a in aggregates if a.method == "infra"]
    assert infra and infra[0].top1 == pytest.approx(1.0)

    results = tmp_path / "phase7"
    written = write_all(rows, results, params=params, allow_overwrite=True)
    assert "summary" in written
    payload = json.loads((results / "summary.json").read_text())
    assert payload["aggregates"]
    assert payload["provenance"]
    assert (results / "README.md").exists()


def test_runner_main_refuses_when_blocked_scenarios_are_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `--allow-blocked`, the dev run refuses because 4 scenarios are blocked.

    This is the D2-Chaos-runtime contract: the harness must not silently score a blocked
    scenario as zero. It refuses and says so.
    """
    monkeypatch.setenv("INFRAMIND_EVAL_FIXTURE_DIR", str(FIXTURES))
    monkeypatch.setenv("INFRAMIND_EVAL_RESULTS_DIR", str(tmp_path / "phase7"))
    monkeypatch.setenv("INFRAMIND_EVAL_ALLOW_BLOCKED", "false")

    exit_code = main(["--set", "dev"])
    assert exit_code == 3, f"expected the blocked-scenario refusal, got {exit_code}"


def test_runner_main_dry_run_lists_scenarios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dry-run` prints the plan and exits 0 without replaying or writing anything."""
    monkeypatch.setenv("INFRAMIND_EVAL_FIXTURE_DIR", str(FIXTURES))
    monkeypatch.setenv("INFRAMIND_EVAL_RESULTS_DIR", str(tmp_path / "phase7"))
    monkeypatch.setenv("INFRAMIND_EVAL_ALLOW_BLOCKED", "true")

    exit_code = main(["--set", "dev", "--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "dev-podchaos-01-cartservice-pod-kill" in out
    assert "[blocked]" in out
    assert not (tmp_path / "phase7").exists(), "--dry-run must not write results"


def test_blocked_scenarios_are_all_stress_or_network_chaos() -> None:
    """Every blocked scenario needs StressChaos or NetworkChaos — the D2-Chaos-runtime set."""
    all_blocked = blocked(load_set("dev")) + blocked(load_set("test"))
    assert all(s.chaos.kind in ("StressChaos", "NetworkChaos") for s in all_blocked)
    assert all(s.block_reason == "d2_chaos_runtime_docker" for s in all_blocked)
