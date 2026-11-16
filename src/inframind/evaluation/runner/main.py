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
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from inframind.common.logging import configure_logging, get_logger
from inframind.common.signal import Source
from inframind.detection.call_error import grpc_callee
from inframind.detection.error_spike import is_error_event

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
from .replay import (
    ReplayTrace,
    has_signals,
    make_run_id,
    replay_run,
    replay_trace,
)

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


def _callee_of(operation: object, services: frozenset[str]) -> str | None:
    """The service a client span's gRPC `operation` called, if it is one we know.

    Same parser the `call_error` detector uses (`detection.call_error.grpc_callee`).
    """
    callee = grpc_callee(operation)
    return callee if callee in services else None


def _graph_depths(trace: ReplayTrace) -> dict[str, int]:
    """Hops from the nearest root (a service nobody calls) in the recorded call graph."""
    graph = trace.graph
    nodes = set(graph.services())
    frontier = [s for s in sorted(nodes) if not graph.predecessors(s)]
    depth = {s: 0 for s in frontier}
    while frontier:
        nxt: list[str] = []
        for service in frontier:
            for callee in graph.successors(service):
                if callee not in depth:
                    depth[callee] = depth[service] + 1
                    nxt.append(callee)
        frontier = nxt
    return depth


def _baseline_window(trace: ReplayTrace) -> tuple[datetime, datetime]:
    """The incident window when one opened, else the whole recording (never the oracle onset)."""
    if trace.incidents:
        return trace.incident_window
    return (trace.signals[0].ts, trace.signals[-1].ts)


def _baseline_input_from_trace(trace: ReplayTrace) -> BaselineInput:
    """Assemble what the D7 baselines may see from the same replay InfraMind ranked.

    Services in play are the application's (any non-alert signal); evidence is counted
    inside the incident window only: error events per service, the depth of the deepest
    erroring span pointing at each service (a failing client span points at its callee),
    and the services Alertmanager alerted on.
    """
    services = frozenset(s.service for s in trace.signals if s.source is not Source.ALERTMANAGER)
    start, end = _baseline_window(trace)
    in_window = [s for s in trace.signals if start <= s.ts <= end]
    depths = _graph_depths(trace)

    errors: dict[str, int] = {}
    error_depth: dict[str, int] = {}
    alerts_by_service: dict[str, list[dict[str, Any]]] = {}
    for signal in in_window:
        if signal.source is Source.ALERTMANAGER:
            if signal.service in services:
                alerts_by_service.setdefault(signal.service, []).append({"service": signal.service})
            continue
        if not is_error_event(signal):
            continue
        errors[signal.service] = errors.get(signal.service, 0) + 1
        if signal.source is not Source.JAEGER:
            continue
        own = depths.get(signal.service, 0)
        target, level = signal.service, own
        callee = (
            _callee_of(signal.attrs.get("operation"), services)
            if signal.attrs.get("kind") == "client"
            else None
        )
        if callee is not None:
            target, level = callee, max(depths.get(callee, 0), own + 1)
        error_depth[target] = max(error_depth.get(target, 0), level)

    return BaselineInput(
        services=tuple(sorted(services)),
        alerts_by_service=alerts_by_service,
        error_events_by_service=errors,
        error_depth_by_service=error_depth,
    )


def _heuristic_baselines(run_seed: int) -> tuple[object, ...]:
    """Return the four baselines that only need a `BaselineInput`.

    `pagerank` and `llm_only` are constructed separately: PageRank needs the full anomaly
    list, and `llm_only` needs the summary text. `run_seed` differs per repeat, so the
    random baseline draws a fresh order each run instead of one order for all five.
    """
    return (
        RawAlertmanagerBaseline(),
        RandomServiceBaseline(seed=run_seed),
        HighestErrorServiceBaseline(),
        DeepestErrorSpanBaseline(),
    )


def _run_baselines(
    *,
    spec: ScenarioSpec,
    seed: int,
    repeat: int,
    baseline_input: BaselineInput,
    trace: ReplayTrace | None = None,
    summary_text: str = "",
) -> list[ResultRow]:
    """Run the D7 baselines over one replay. Returns one row per baseline that ran.

    PageRank runs the P5 `rank_pagerank` on the trace's incident (same symptoms, anomalies,
    graph and window InfraMind ranked); with no incident it ranks nothing. `llm_only` needs
    an LLM answer recorded for this run; without one it is skipped and logged, never scored
    from a placeholder.
    """
    from ..baselines import PAGERANK_METHOD, rank_from_pagerank

    def row(method: str, ranked: Sequence[str]) -> ResultRow:
        return default_scorer(
            spec,
            seed,
            repeat,
            method=method,
            method_kind="baseline",
            run_id=make_run_id(spec=spec, seed=seed, repeat=repeat, method=method),
            detected=bool(ranked),
            ranked=list(ranked),
            detection_latency_s=None,
            false_positive=False,
            blocked=False,
        )

    rows = [
        row(baseline.name, baseline.rank(baseline_input))  # type: ignore[attr-defined]
        for baseline in _heuristic_baselines(seed * 1000 + repeat)
    ]

    pagerank_ranked: list[str] = []
    if trace is not None and trace.incidents:
        pagerank_ranked = rank_from_pagerank(
            symptoms=trace.symptoms,
            anomalies=trace.incident_anomalies,
            graph=trace.graph,
            window=trace.incident_window,
        )
    rows.append(row(PAGERANK_METHOD, pagerank_ranked))

    if summary_text:
        rows.append(
            row("llm_only", LLmOnlyBaseline().rank(baseline_input, summary_text=summary_text))
        )
    else:
        log.info(
            "baseline_not_run",
            baseline="llm_only",
            scenario_id=spec.scenario_id,
            repeat=repeat,
            reason="no LLM answer recorded for this run",
        )
    return rows


def _no_fault(spec: ScenarioSpec, fixture_dir: Path, repeat: int) -> bool:
    """True (and logged) when the recording says its fault never took effect.

    `meta.json` `"fault_injected": false` marks a repeat whose fault the runtime refused
    (dev-02's container-kill, D26). Scoring it would credit or blame a method for a fault
    that did not happen, so it yields no row, even if the scenario is unblocked later.
    """
    import json

    meta_path = fixture_dir / "meta.json"
    if not meta_path.exists() or json.loads(meta_path.read_text()).get("fault_injected", True):
        return False
    log.warning("fixture_no_fault", scenario_id=spec.scenario_id, repeat=repeat)
    return True


def _dirty_baseline(spec: ScenarioSpec, fixture_dir: Path, repeat: int) -> bool:
    """True (and logged) when a live recording's baseline cannot be scored (`baseline_problem`).

    Only live recordings are checked (`recorded_live` in `meta.json`); a dirty one yields no
    row, so the report's coverage shows the gap instead of a contaminated score.
    """
    import json

    from .replay import baseline_problem

    meta_path = fixture_dir / "meta.json"
    if not meta_path.exists() or not json.loads(meta_path.read_text()).get("recorded_live"):
        return False
    problem = baseline_problem(replay_trace(fixture_dir, params=EvalParams()))
    if problem:
        log.warning(
            "fixture_dirty_baseline",
            scenario_id=spec.scenario_id,
            repeat=repeat,
            problem=problem,
            hint="re-record this repeat",
        )
    return problem is not None


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
    if not has_signals(fixture_dir):
        log.warning(
            "fixture_missing",
            scenario_id=spec.scenario_id,
            repeat=repeat,
            fixture_dir=str(fixture_dir),
        )
        return []
    if _no_fault(spec, fixture_dir, repeat) or _dirty_baseline(spec, fixture_dir, repeat):
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

    if run_baselines:
        trace = replay_trace(fixture_dir, params=params)
        rows.extend(
            _run_baselines(
                spec=spec,
                seed=seed,
                repeat=repeat,
                baseline_input=_baseline_input_from_trace(trace),
                trace=trace,
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
            if has_signals(target) and not params.allow_overwrite:
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
