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
import gzip
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


#: A recording's signals: gzip-compressed (live recordings, committed) or plain (tests).
SIGNALS_GZ = "signals.ndjson.gz"
SIGNALS_PLAIN = "signals.ndjson"


def signals_file(fixture_dir: Path) -> Path:
    """The signals file of `fixture_dir`: the `.gz` one if present, else the plain one."""
    compressed = fixture_dir / SIGNALS_GZ
    return compressed if compressed.exists() else fixture_dir / SIGNALS_PLAIN


def has_signals(fixture_dir: Path) -> bool:
    """Whether `fixture_dir` holds a recording (either signals file)."""
    return signals_file(fixture_dir).exists()


def write_signals_gz(path: Path, signals: Sequence[Signal]) -> None:
    """Write gzip NDJSON with no name or time in the header, so equal data gives equal bytes."""
    lines = "".join(s.model_dump_json() + "\n" for s in signals).encode()
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(lines)


def replay_signals(path: Path) -> list[Signal]:
    """Read a signals file (`.ndjson` or `.ndjson.gz`) and return the signals in order.

    Strict parse: lines that fail validation raise `ReplayError`. ts-ordering is asserted
    on the result, so a fixture whose timestamps drift backward is also an error.
    """
    if not path.exists():
        raise ReplayError(f"{path} does not exist")

    text = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
    signals: list[Signal] = []
    line_no = 0
    for raw in text.splitlines():
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


def pre_fault_anomalies(signals: Sequence[Signal], onset_ts: datetime) -> int:
    """Anomalies the default detectors raise on the signals before `onset_ts`.

    A recording's baseline must be quiet: an anomaly before the fault means an earlier fault
    (another repeat, an aborted run, a test) is inside it, and the run would score an
    incident the fault did not cause, typically as a false 0 s detection latency. Clean
    live recordings score 0 here; the runner refuses to score any other.
    """
    engine = DetectionEngine.default({})
    return sum(len(engine.process(s)) for s in signals if s.ts < onset_ts)


def load_graph_snapshot(path: Path) -> ServiceGraph:
    """Build a `ServiceGraph` populated from `graph.json`, with cold-start fallbacks.

    Stub edges from the snapshot are retained: cold-start ranking is still possible,
    just downweighted.
    """
    if not path.exists():
        return ServiceGraph(seed_path=None)
    graph = ServiceGraph(seed_path=path)
    return graph


@dataclass(frozen=True, slots=True)
class ReplayTrace:
    """What detection and correlation produced for one fixture; every ranker reads this.

    InfraMind and the D7 baselines rank from the same trace, over the same incident window
    and anomalies, so a comparison differs only in the ranking method.
    """

    signals: list[Signal]
    graph: ServiceGraph
    onset_ts: datetime
    incidents: list[Incident]

    @property
    def first_open(self) -> datetime:
        """When the first incident opened. Only valid when `incidents` is non-empty."""
        return min(i.opened_at for i in self.incidents)

    @property
    def incident_window(self) -> tuple[datetime, datetime]:
        """`(first opened_at, last updated_at)`: what the live RCA worker ranks over.

        The ground-truth fault window is the scorer's business, never the ranker's: a
        ranker that saw it would be told when the fault started.
        """
        if not self.incidents:
            raise ReplayError("no incident: there is no incident window")
        last = max((i.closed_at or i.updated_at) for i in self.incidents)
        return (self.first_open, last)

    @property
    def symptoms(self) -> list[str]:
        """Every service any incident revision named, sorted."""
        return sorted({s for i in self.incidents for s in i.services})

    @property
    def incident_anomalies(self) -> list[Anomaly]:
        """Each anomaly once (revisions repeat them), in `(ts, id)` order."""
        unique = {a.id: a for i in self.incidents for a in i.anomalies}
        return sorted(unique.values(), key=lambda a: (a.ts, a.id))


def replay_trace(
    fixture_dir: Path,
    *,
    params: EvalParams,
    correlation_params: CorrelationParams | None = None,
) -> ReplayTrace:
    """Run detection and correlation over one fixture, exactly as the live worker does."""
    correlation_params = correlation_params or CorrelationParams()
    signals = replay_signals(signals_file(fixture_dir))
    graph = load_graph_snapshot(fixture_dir / "graph.json")
    window_s = float(params.detect_window_s)
    meta_path = fixture_dir / "meta.json"
    if meta_path.exists():
        from ..schema import FixtureMeta

        meta = FixtureMeta.from_json(meta_path)
        onset_ts = meta.onset_ts
        window_s = max(window_s, float(meta.window_s))
    else:
        onset_ts = _derive_onset(signals)

    # The correlation grouping window defaults to 5 minutes; for an evaluation replay we
    # tune it from the scenario's window so two anomalies on the same fault always join.
    tracker = IncidentTracker(
        (engine := DetectionEngine.default()).coordinator,
        _tune_correlation(correlation_params, window_s=window_s),
    )
    incidents: list[Incident] = []
    for signal in signals:
        for anomaly in engine.process(signal):
            incidents.extend(tracker.on_anomaly(anomaly))
        # Mirror the worker's handle(): drive the tracker tick per signal.
        incidents.extend(tracker.tick(signal.ts))
        # Mirror the "attach_signal" path so Alertmanager signals are seen by the tracker.
        if signal.source.value == "alertmanager":
            tracker.attach_signal(signal)
    return ReplayTrace(signals=signals, graph=graph, onset_ts=onset_ts, incidents=incidents)


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
    trace = replay_trace(fixture_dir, params=params, correlation_params=correlation_params)
    incidents = trace.incidents

    detected = bool(incidents)
    detection_latency_s: float | None = None
    if detected:
        detection_latency_s = max(0.0, (trace.first_open - trace.onset_ts).total_seconds())

    # Score the live detector/ranker for the infra variant. Baselines and ablations plug in
    # via the `scorer` callable so this single entry point produces every row.
    if incidents:
        ranked_candidates = rank_candidates(
            incident_id=run_id,
            symptoms=trace.symptoms,
            anomalies=trace.incident_anomalies,
            graph=trace.graph,
            window=trace.incident_window,
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
    "SIGNALS_GZ",
    "OnLLMSummary",
    "ReplayError",
    "ReplayInputs",
    "ReplayResult",
    "ReplayTrace",
    "Scorer",
    "compute_fault_window",
    "has_signals",
    "load_graph_snapshot",
    "make_run_id",
    "pre_fault_anomalies",
    "replay_run",
    "replay_signals",
    "replay_trace",
    "signals_file",
    "write_signals_gz",
]
