"""Replay path: feed an NDJSON fixture through the pipeline deterministically (standard 10.4).

The pipeline is a function of signal timestamps (D6). Given the same `Signal` sequence at
the same `ts`, detection, correlation, and ranking always produce the same ranking, the
same incident-open time, and the same candidates. So a run is reproducible from
`evaluation/fixtures/<scenario>/signals.ndjson` plus the graph snapshot — no Kubernetes.

Two public entry points:

* `replay_signals(path) -> list[Signal]` — strict parse. Lines that fail validation raise
  immediately (a corrupt fixture is a load error, not a silent skip).
* `replay_run(fixture_dir, *, params, scorer)` — orchestrates one fixture through
  detection → correlation → ranking, then scores the ranking against `scenario.expected_root_cause`.

`live.py` lives next to this and is the cluster-touching counterpart. It refuses to run
against a non-`kind-` context and refuses blocked scenarios unless `allow_blocked`.

Replays never write fixtures — recording lands with the first real P7 run.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from inframind.common.anomaly import Anomaly
from inframind.common.exceptions import InfraMindError
from inframind.common.incident import Incident
from inframind.common.signal import Signal
from inframind.correlation.config import CorrelationParams
from inframind.correlation.tracker import IncidentTracker
from inframind.detection.engine import DetectionEngine
from inframind.rca.config import RCAParams
from inframind.rca.graph import ServiceGraph
from inframind.rca.rank import rank_candidates

from ..config import EvalParams
from ..schema import ResultRow, ScenarioSpec


class Scorer(Protocol):
    """Anything that can take a fixture + candidates and return a `ResultRow`."""

    def __call__(
        self,
        spec: ScenarioSpec,
        seed: int,
        repeat: int,
        *,
        method: str,
        method_kind: str,
        run_id: str,
        detected: bool,
        ranked: Sequence[str],
        detection_latency_s: float | None,
        false_positive: bool,
        blocked: bool,
    ) -> ResultRow: ...


@dataclass(frozen=True, slots=True)
class ReplayInputs:
    """What `replay_run` needs from a fixture directory to reproduce a run."""

    signals: list[Signal]
    graph: ServiceGraph
    onset_ts: datetime
    window_s: float
    fault_window: tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """What one fixture replay produced, plus what `scoring.py` needs to record a row."""

    detected: bool
    ranked: tuple[str, ...]
    detection_latency_s: float | None
    false_positive: bool
    top_anomaly_ts: datetime | None = field(default=None, repr=False)

    def as_tuple(self) -> tuple[bool, tuple[str, ...], float | None, bool, datetime | None]:
        """Tuple form, for callers that want to destructure without naming fields."""
        return (
            self.detected,
            self.ranked,
            self.detection_latency_s,
            self.false_positive,
            self.top_anomaly_ts,
        )


def replay_signals(path: Path) -> list[Signal]:
    """Read `signals.ndjson` from `path` and return the signals in order.

    Strict parse: lines that fail validation raise `ReplayError`. ts-ordering is asserted
    on the result, so a fixture whose timestamps drift backward is also an error.
    """
    if not path.exists():
        raise ReplayError(f"{path} does not exist")

    signals: list[Signal] = []
    line_no = 0
    for raw in path.read_text().splitlines():
        line_no += 1
        if not raw.strip():
            continue
        try:
            decoded = json.loads(raw)
            signals.append(Signal.model_validate(decoded))
        except (json.JSONDecodeError, ValueError) as e:
            raise ReplayError(f"{path}:{line_no} invalid signal: {e}") from e

    for prev, curr in itertools.pairwise(signals):
        if curr.ts < prev.ts:
            raise ReplayError(f"{path}: ts not monotone (line {line_no}: {prev.ts} > {curr.ts})")
    return signals


def load_graph_snapshot(path: Path) -> ServiceGraph:
    """Build a `ServiceGraph` populated from `graph.json`, with cold-start fallbacks.

    Stub edges from the snapshot are retained: cold-start ranking is still possible,
    just downweighted.
    """
    if not path.exists():
        return ServiceGraph(seed_path=None)
    graph = ServiceGraph(seed_path=path)
    return graph


def compute_fault_window(onset_ts: datetime, duration_s: float) -> tuple[datetime, datetime]:
    """Return `(start, end)` of the fault window, the bracket the scorer compares against."""
    return (onset_ts, _add_seconds(onset_ts, duration_s))


def replay_run(
    fixture_dir: Path,
    *,
    spec: ScenarioSpec,
    scorer: Scorer,
    method: str,
    method_kind: str,
    run_id: str,
    seed: int,
    repeat: int,
    params: EvalParams,
    rca_params: RCAParams | None = None,
    correlation_params: CorrelationParams | None = None,
    on_llm_summary: OnLLMSummary | None = None,
) -> ResultRow:
    """Replay one fixture through detection → correlation → ranking, return a scored row.

    The pipeline is built from the SAME detector / tracker / ranker modules a live run uses;
    replays are not stubs. Detection runs in-process, correlation in-process, ranking
    in-process. The bus is never touched.
    """
    rca_params = rca_params or RCAParams()
    correlation_params = correlation_params or CorrelationParams()
    signals_path = fixture_dir / "signals.ndjson"
    graph_path = fixture_dir / "graph.json"
    meta_path = fixture_dir / "meta.json"

    signals = replay_signals(signals_path)
    graph = load_graph_snapshot(graph_path)
    window_s = float(params.detect_window_s)
    if meta_path.exists():
        from ..schema import FixtureMeta

        meta = FixtureMeta.from_json(meta_path)
        onset_ts = meta.onset_ts
        window_s = max(window_s, float(meta.window_s))
    else:
        onset_ts = _derive_onset(signals)

    # The correlation grouping window defaults to 5 minutes; for an evaluation replay we
    # tune it from the scenario's window so two anomalies on the same fault always join.
    tuned_correlation = _tune_correlation(correlation_params, window_s=window_s)
    engine = DetectionEngine.default()
    tracker = IncidentTracker(engine.coordinator, tuned_correlation)
    anomalies: list[Anomaly] = []
    incidents: list[Incident] = []
    for signal in signals:
        for anomaly in engine.process(signal):
            anomalies.append(anomaly)
            snapshots = tracker.on_anomaly(anomaly)
            incidents.extend(snapshots)
        # Mirror the worker's handle() — drive the tracker tick per signal.
        incidents.extend(tracker.tick(signal.ts))
        # Mirror the "attach_signal" path so Alertmanager signals are seen by the tracker.
        if signal.source.value == "alertmanager":
            tracker.attach_signal(signal)

    detected = bool(incidents)
    detection_latency_s: float | None = None
    if detected:
        first_open = min(i.opened_at for i in incidents if i.state == "open")
        detection_latency_s = max(0.0, (first_open - onset_ts).total_seconds())

    # Score the live detector/ranker for the infra variant. Baselines and ablations plug in
    # via the `scorer` callable so this single entry point produces every row.
    if incidents:
        symptoms = sorted({s for i in incidents for s in i.services})

        incident_anomalies = [a for i in incidents for a in i.anomalies]
        window = compute_fault_window(onset_ts, spec.duration_s)
        ranked_candidates = rank_candidates(
            incident_id=run_id,
            symptoms=symptoms,
            anomalies=incident_anomalies,
            graph=graph,
            window=window,
            params=rca_params,
        )
        ranked_services = tuple(c.service for c in ranked_candidates)

        # D1 invariant check: ranking must not depend on the LLM. The no-llm ablation
        # exercises this by *skipping* `explain`; here we only call it on demand.
        if on_llm_summary is not None:
            try:
                summary, _provenance = on_llm_summary(
                    incidents[0],
                    ranked_candidates,
                    scenario_id=spec.scenario_id,
                )
                # If a future ranking depended on `summary`, that is a D1 violation caught
                # by the no-llm ablation. The harness never uses the summary for ranking.
                _ = summary
            except Exception:
                pass
    else:
        ranked_services = ()

    # False-positive: a fault-free scenario (no `expected_root_cause`) should not open one.
    false_positive = bool(incidents) and not spec.expected_root_cause
    # ... and the no-llm invariant: the LLM cannot change detection or ranking.
    # The scorer is the seam that enforces this for baselines and ablations.

    return scorer(
        spec,
        seed,
        repeat,
        method=method,
        method_kind=method_kind,
        run_id=run_id,
        detected=detected,
        ranked=ranked_services,
        detection_latency_s=detection_latency_s,
        false_positive=false_positive,
        blocked=False,
    )


# --- helpers ---


def _add_seconds(ts: datetime, seconds: float) -> datetime:
    from datetime import timedelta

    return ts + timedelta(seconds=seconds)


def _tune_correlation(params: CorrelationParams, *, window_s: float) -> CorrelationParams:
    """Return `params` with `window_s` widened to at least `window_s` seconds.

    The default correlation window (5 minutes) is fine for live traffic but tight for
    evaluation, where the recorder may pause a few seconds between signals. Bumping it to
    span the whole fault window ensures two anomalies on the same fault always join a single
    incident.
    """
    if window_s <= params.window_s:
        return params
    return dataclasses.replace(params, window_s=window_s)


def _derive_onset(signals: Sequence[Signal]) -> datetime:
    """Best-effort onset: the first anomaly-time-shaped signal (Alertmanager-like).

    Falls back to the first signal's `ts` if nothing looks like an alert.
    """
    for signal in signals:
        if signal.source.value == "alertmanager":
            return signal.ts
    return signals[0].ts if signals else datetime.now(UTC)


class OnLLMSummary(Protocol):
    """Anything with the same signature as `inframind.llm.explain`."""

    def __call__(
        self,
        incident: Incident,
        candidates: Sequence[Any],
        *,
        scenario_id: str,
    ) -> tuple[Any, Any]: ...


class ReplayError(InfraMindError):
    """A fixture is missing, malformed, or violates a replay invariant."""


def make_run_id(*, spec: ScenarioSpec, seed: int, repeat: int, method: str) -> str:
    """Build a provenance id for one run, of the form `<scenario>@<seed>r<repeat>:<method>`.

    The `run_id` is what `report.py` uses to tie a row back to its fixture, so it must be
    derivable from `(scenario_id, seed, repeat, method)` without consulting external state.
    """
    return f"{spec.scenario_id}@{seed}r{repeat}:{method}"


__all__ = [
    "OnLLMSummary",
    "ReplayError",
    "ReplayInputs",
    "ReplayResult",
    "Scorer",
    "compute_fault_window",
    "load_graph_snapshot",
    "make_run_id",
    "replay_run",
    "replay_signals",
]
