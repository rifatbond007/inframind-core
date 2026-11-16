"""The eval CLI entry point: one `python -m inframind.evaluation.runner.main` per run.

Orchestration, nothing more:

1. Load `EvalParams` from the environment.
2. Load the scenario set(s) the `--set` flag names.
3. Refuse blocked scenarios unless `--allow-blocked`.
4. Replay each fixture through `replay_run` for the `infra` variant.
5. Run each baseline and each ablation over the same fixture.
6. Score, aggregate, write the report.

`--live` records instead of scoring: for every selected scenario and repeat it injects the
fault on the kind testbed and writes `evaluation/fixtures/<scenario>/r<repeat>/`
(`runner/live.py`); a later replay scores those recordings. It is human-run only
(`make eval-live`), and refuses any context that is not `kind-*`.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from inframind.common.incident import Incident
from inframind.common.logging import configure_logging, get_logger
from inframind.common.signal import Signal, Source

from .. import report as report_mod
from ..baselines import (
    BaselineInput,
    DeepestErrorSpanBaseline,
    HighestErrorServiceBaseline,
    LLmOnlyBaseline,
    RandomServiceBaseline,
    RawAlertmanagerBaseline,
)
from ..config import EvalParams, eval_set_or_all, load_eval_params
from ..scenarios import ScenarioError, load_set, runnable
from ..schema import ResultRow, ScenarioSpec
from ..scoring import default_scorer
from .replay import make_run_id, replay_run

log = get_logger(__name__)

#: The method name InfraMind's own pipeline produces.
INFRA_METHOD = "infra"

#: The soak method name: the fault-free window run that measures the FPR.
SOAK_METHOD = "soak"


def build_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m inframind.evaluation.runner.main",
        description="Replay InfraMind evaluation scenarios and score against the D7 baselines.",
    )
    parser.add_argument("--set", default=None, help="dev | test | all (default: dev)")
    parser.add_argument("--soak", action="store_true", help="run the fault-free soak window")
    parser.add_argument("--ablation", action="store_true", help="run the four ablations")
    parser.add_argument("--no-baselines", action="store_true", help="skip the six D7 baselines")
    parser.add_argument(
        "--allow-blocked",
        action="store_true",
        help="score blocked scenarios as `blocked: true` rows instead of refusing them",
    )
    parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="overwrite an existing results directory",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="run against a real cluster (kind-* only). Not wired to any Make target.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=None,
        help="only this scenario_id (repeatable); applies to replay and --live",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="load scenarios and print the plan without replaying",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse `argv` (or `sys.argv[1:]` when None)."""
    return build_parser().parse_args(argv)


def _baseline_input_from_replay(
    *, signals: Sequence[Signal], incidents: Sequence[Incident]
) -> BaselineInput:
    """Assemble the `BaselineInput` a baseline sees.

    Baseline access is deliberately narrower than the InfraMind path: services in play,
    per-service signal payloads, per-service anomaly severity, and trace depth. The
    `pagerank` baseline is the one exception — it is wired by the runner with the real
    `Anomaly` objects, because it is a re-export of the P5 ranker rather than a heuristic.
    """
    services: set[str] = set()
    signals_by_service: dict[str, list[dict[str, Any]]] = {}
    trace_depth: dict[str, int] = {}
    alerts_by_service: dict[str, list[dict[str, Any]]] = {}

    for signal in signals:
        services.add(signal.service)
        signals_by_service.setdefault(signal.service, []).append(
            {"attrs": signal.attrs, "source": signal.source.value, "type": signal.type.value}
        )
        if signal.source is Source.ALERTMANAGER:
            alerts_by_service.setdefault(signal.service, []).append(
                {"instance": signal.service, "service": signal.service}
            )
        depth = signal.attrs.get("trace_depth")
        if depth is not None:
            with contextlib.suppress(TypeError, ValueError):
                trace_depth[signal.service] = max(trace_depth.get(signal.service, 0), int(depth))

    for incident in incidents:
        services.update(incident.services)

    ordered = tuple(sorted(services))
    return BaselineInput(
        services=ordered,
        signals_by_service=signals_by_service,
        trace_depth_by_service=trace_depth,
        alerts_by_service=alerts_by_service,
    )


def _heuristic_baselines() -> tuple[object, ...]:
    """Return the four baselines that only need a `BaselineInput`.

    `pagerank` and `llm_only` are constructed separately: PageRank needs the full anomaly
    list, and `llm_only` needs the summary text.
    """
    return (
        RawAlertmanagerBaseline(),
        RandomServiceBaseline(),
        HighestErrorServiceBaseline(),
        DeepestErrorSpanBaseline(),
    )


def _run_baselines(
    *,
    spec: ScenarioSpec,
    seed: int,
    repeat: int,
    baseline_input: BaselineInput,
    pagerank_services: Sequence[str] | None = None,
    summary_text: str = "",
) -> list[ResultRow]:
    """Run the six D7 baselines over one replay. Returns one row per baseline."""
    from ..baselines import PAGERANK_METHOD

    rows: list[ResultRow] = []
    for baseline in _heuristic_baselines():
        ranked = baseline.rank(baseline_input)  # type: ignore[attr-defined]
        rows.append(
            default_scorer(
                spec,
                seed,
                repeat,
                method=baseline.name,  # type: ignore[attr-defined]
                method_kind="baseline",
                run_id=make_run_id(spec=spec, seed=seed, repeat=repeat, method=baseline.name),  # type: ignore[attr-defined]
                detected=bool(ranked),
                ranked=ranked,
                detection_latency_s=None,
                false_positive=False,
                blocked=False,
            )
        )

    # PageRank re-exports the P5 ranker. The runner passes the ranked services the real
    # `rank_pagerank` produced (from `_run_one`); with no anomalies to work from it falls
    # back to alphabetical order, which `report.py` marks via the `notes` field.
    pagerank_ranked = (
        list(pagerank_services) if pagerank_services else sorted(baseline_input.services)
    )
    rows.append(
        default_scorer(
            spec,
            seed,
            repeat,
            method=PAGERANK_METHOD,
            method_kind="baseline",
            run_id=make_run_id(spec=spec, seed=seed, repeat=repeat, method=PAGERANK_METHOD),
            detected=bool(pagerank_ranked),
            ranked=pagerank_ranked,
            detection_latency_s=None,
            false_positive=False,
            blocked=False,
        )
    )

    rows.append(
        default_scorer(
            spec,
            seed,
            repeat,
            method="llm_only",
            method_kind="baseline",
            run_id=make_run_id(spec=spec, seed=seed, repeat=repeat, method="llm_only"),
            detected=bool(baseline_input.services),
            ranked=LLmOnlyBaseline().rank(baseline_input, summary_text=summary_text),
            detection_latency_s=None,
            false_positive=False,
            blocked=False,
        )
    )
    return rows


def _dirty_baseline(spec: ScenarioSpec, fixture_dir: Path, repeat: int) -> bool:
    """True (and logged) when a live recording's baseline holds an anomaly (see replay).

    Only live recordings are checked (`recorded_live` in `meta.json`); a dirty one yields no
    row, so the report's coverage shows the gap instead of a contaminated score.
    """
    import json

    from ..schema import FixtureMeta
    from .replay import pre_fault_anomalies, replay_signals

    meta_path = fixture_dir / "meta.json"
    if not meta_path.exists() or not json.loads(meta_path.read_text()).get("recorded_live"):
        return False
    onset = FixtureMeta.from_json(meta_path).onset_ts
    found = pre_fault_anomalies(replay_signals(fixture_dir / "signals.ndjson"), onset)
    if found:
        log.warning(
            "fixture_dirty_baseline",
            scenario_id=spec.scenario_id,
            repeat=repeat,
            pre_fault_anomalies=found,
            hint="an earlier fault is inside this recording's baseline; re-record it",
        )
    return found > 0


def _run_one(
    *,
    spec: ScenarioSpec,
    params: EvalParams,
    repeat: int,
    run_baselines: bool,
    run_ablations: bool,
    method: str = INFRA_METHOD,
) -> list[ResultRow]:
    """Replay one fixture for one (scenario, seed, repeat). Returns the infra + extras rows."""
    from ..scenarios import fixture_dir_for

    seed = params.seed_override if params.seed_override is not None else spec.seed
    fixture_dir = fixture_dir_for(spec, params, repeat)
    if not (fixture_dir / "signals.ndjson").exists():
        log.warning(
            "fixture_missing",
            scenario_id=spec.scenario_id,
            repeat=repeat,
            fixture_dir=str(fixture_dir),
        )
        return []
    if _dirty_baseline(spec, fixture_dir, repeat):
        return []

    run_id = make_run_id(spec=spec, seed=seed, repeat=repeat, method=method)
    infra_row = replay_run(
        fixture_dir,
        spec=spec,
        scorer=default_scorer,
        method=method,
        method_kind="infra",
        run_id=run_id,
        seed=seed,
        repeat=repeat,
        params=params,
    )
    rows = [infra_row]

    if run_baselines or run_ablations:
        from .replay import replay_signals

        signals = replay_signals(fixture_dir / "signals.ndjson")
        baseline_input = _baseline_input_from_replay(signals=signals, incidents=[])
        if run_baselines:
            infra_predicted = (
                infra_row.predicted_root_cause.split("|") if infra_row.predicted_root_cause else []
            )
            rows.extend(
                _run_baselines(
                    spec=spec,
                    seed=seed,
                    repeat=repeat,
                    baseline_input=baseline_input,
                    pagerank_services=infra_predicted or None,
                    summary_text="",
                )
            )

    return rows


def _record_live(specs: Sequence[ScenarioSpec], params: EvalParams) -> int:
    """Record one fixture per (scenario, repeat) that has none yet. Returns an exit code."""
    from ..scenarios import fixture_dir_for
    from .live import LiveRunner, LiveRunnerError

    try:
        runner = LiveRunner(params=params)
    except LiveRunnerError as e:
        log.error("live_runner_refused", error=str(e))
        return 5
    for spec in specs:
        if spec.is_blocked and not params.allow_blocked:
            log.info("live_skip_blocked", scenario_id=spec.scenario_id, reason=spec.block_reason)
            continue
        for repeat in range(params.repeats):
            target = fixture_dir_for(spec, params, repeat)
            if (target / "signals.ndjson").exists() and not params.allow_overwrite:
                log.info("live_fixture_exists", scenario_id=spec.scenario_id, repeat=repeat)
                continue
            try:
                recording = runner.run_scenario(
                    spec,
                    repeat=repeat,
                    record_to=target,
                    allow_blocked=params.allow_blocked,
                    overwrite=params.allow_overwrite,
                )
            except LiveRunnerError as e:
                log.error("live_record_failed", scenario_id=spec.scenario_id, error=str(e))
                return 5
            print(
                f"recorded {spec.scenario_id} r{repeat}: {recording.signal_count} signals, "
                f"{recording.edge_count} edges -> {target}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the eval. Returns the process exit code."""
    args = parse_args(argv)
    configure_logging()
    params = load_eval_params()
    # CLI flags override the env-derived params.
    if args.allow_blocked:
        from dataclasses import replace

        params = replace(params, allow_blocked=True)
    if args.allow_overwrite:
        from dataclasses import replace

        params = replace(params, allow_overwrite=True)
    if args.ablation:
        params = replace(params, run_ablations=True) if hasattr(params, "run_ablations") else params

    sets = eval_set_or_all(args.set)

    all_rows: list[ResultRow] = []
    for scenario_set in sets:
        try:
            specs = load_set(scenario_set)
        except ScenarioError as e:
            log.error("scenario_load_failed", scenario_set=scenario_set, error=str(e))
            return 2

        if args.scenario:
            specs = [s for s in specs if s.scenario_id in set(args.scenario)]
        runnable_specs = runnable(specs)
        blocked_count = len(specs) - len(runnable_specs)
        log.info(
            "scenario_set_loaded",
            scenario_set=scenario_set,
            total=len(specs),
            runnable=len(runnable_specs),
            blocked=blocked_count,
        )

        if args.live:  # recording skips blocked scenarios (logged) rather than refusing
            code = _record_live(specs, params)
            if code:
                return code
            continue

        if blocked_count and not params.allow_blocked:
            log.error(
                "blocked_scenarios_present",
                count=blocked_count,
                hint="set INFRAMIND_EVAL_ALLOW_BLOCKED=1 to record blocked rows",
            )
            return 3

        if args.dry_run:
            for spec in specs:
                print(f"  {spec.scenario_id}  [{spec.status}]  {spec.fault_type}")
            continue

        for spec in specs:
            if spec.is_blocked and not params.allow_blocked:
                continue
            for repeat in range(params.repeats):
                rows = _run_one(
                    spec=spec,
                    params=params,
                    repeat=repeat,
                    run_baselines=params.run_baselines and not args.no_baselines,
                    run_ablations=args.ablation,
                )
                all_rows.extend(rows)

    if args.dry_run or args.live:
        return 0

    results_dir = Path(params.results_dir)
    try:
        report_mod.write_all(
            all_rows,
            results_dir,
            params=params,
            allow_overwrite=params.allow_overwrite,
        )
    except FileExistsError as e:
        log.error("results_dir_exists", error=str(e))
        return 4

    print(f"wrote {len(all_rows)} rows to {results_dir}")
    return 0


__all__ = ["INFRA_METHOD", "SOAK_METHOD", "build_parser", "main", "parse_args"]


if __name__ == "__main__":
    sys.exit(main())
