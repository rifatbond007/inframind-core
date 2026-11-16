"""Live runner: injects one scenario's fault on the kind testbed and records a fixture.

This module is the **only** place in `evaluation/` that calls `kubectl`. A live run records;
it never scores. Scoring always replays the recorded fixture (D7-Replay), so a number in
the paper is reproducible from `evaluation/fixtures/<scenario>/r<repeat>/` alone.

One recording (`LiveRunner.run_scenario`):

1. **Baseline.** Wait until `stream:signals` holds at least `pre_s` seconds of history
   (`max(spec.warmup_s, EvalParams.baseline_s)`; the detectors need 10 samples and the
   Prometheus collector polls every 30 s, so the default is 600 s), **and** until the end
   of the newest recording under `fixture_dir` is at least `pre_s` in the past, so no
   earlier fault (another repeat or scenario) lies inside this run's baseline window.
   Back-to-back repeats 287 s apart once put the previous kill inside the next baseline:
   the incident opened before the fault and every later repeat scored a false 0 s
   latency.
2. **Inject** at `onset_ts`: apply the Chaos Mesh CR built from `spec.chaos`
   (`build_chaos_manifest`), or apply `spec.config_change` with `kubectl set env`.
3. **Hold** for `duration_s`, then **revert** (delete the CR / `kubectl rollout undo`). The
   revert runs in a `finally`, and every CR also carries a Chaos Mesh `duration`, so a
   crashed recorder cannot leave a fault behind.
4. **Settle** for `cooldown_s + settle_s` so the collectors publish the recovery.
5. **Record** `stream:signals` entries from `onset - pre_s` to the end, sorted by `ts`,
   the service graph folded from every `stream:graph-updates` entry up to the end, and a
   `meta.json` with the onset and window.

Guards:

* **Context must start with `kind-`.** `~/.kube/config` is never read; the kubeconfig is
  `$KUBECONFIG` or the testbed's `testbed/.kube/config`, and every kubectl call pins
  `--context`.
* **Blocked scenarios need an explicit flag** (D26).
* **An existing fixture is never overwritten** unless the caller asks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from inframind.common import stream_keys
from inframind.common.graph_update import GraphUpdate
from inframind.common.logging import get_logger
from inframind.common.signal import Signal
from inframind.common.time import utcnow
from inframind.rca.graph import ServiceGraph

from ..config import EvalParams
from ..scenarios import BLOCKED_REASON
from ..schema import FixtureMeta, ScenarioSpec

log = get_logger(__name__)

KIND_CONTEXT_PREFIX = "kind-"
KUBECONFIG_ENV = "KUBECONFIG"
CHAOS_API_VERSION = "chaos-mesh.org/v1alpha1"

#: Written into a repeat's directory just before its fault is applied and removed once the
#: fixture is complete, so a recorder killed mid-fault still counts that fault in the next
#: run's quiet-baseline wait (`latest_recording_end`).
FAULT_MARKER = "fault-started.json"

#: The bus stores each model as JSON under this field (`common/bus.py`).
_BUS_FIELD = b"data"
_XRANGE_PAGE = 5_000

#: `run(argv, stdin) -> (returncode, stdout, stderr)`; injectable so tests never shell out.
CommandRunner = Callable[[Sequence[str], str | None], tuple[int, str, str]]

__all__ = [
    "FAULT_MARKER",
    "KIND_CONTEXT_PREFIX",
    "BlockedScenarioError",
    "LiveRunner",
    "LiveRunnerError",
    "Recording",
    "WrongKubeContext",
    "build_chaos_manifest",
    "chaos_resource_name",
    "config_change_commands",
    "latest_recording_end",
    "list_blocked_reasons",
    "read_graph",
    "read_signals",
]


class LiveRunnerError(RuntimeError):
    """A kubectl call failed, or the recording could not be written."""


class WrongKubeContext(LiveRunnerError):
    """The active kubeconfig context is not a `kind-*` cluster."""


class BlockedScenarioError(LiveRunnerError):
    """The scenario is blocked by D2-Chaos-runtime and `allow_blocked` is False."""


@dataclass(frozen=True, slots=True)
class Recording:
    """What one live run wrote: where, when the fault started, and how much it captured."""

    fixture_dir: Path
    meta: FixtureMeta
    signal_count: int
    edge_count: int


# --- pure builders (unit-tested without a cluster) -------------------------------------


def chaos_resource_name(spec: ScenarioSpec, repeat: int) -> str:
    """A DNS-1123 name for this run's CR, unique per (scenario, repeat), at most 63 chars."""
    raw = f"eval-{spec.scenario_id}-r{repeat}".lower()
    name = re.sub(r"[^a-z0-9-]+", "-", raw)
    if len(name) > 63:
        suffix = f"-r{repeat}"
        name = name[: 63 - len(suffix)].rstrip("-") + suffix
    return name


def build_chaos_manifest(
    spec: ScenarioSpec, *, name: str, chaos_namespace: str = "chaos-testing"
) -> dict[str, Any]:
    """Return the full Chaos Mesh CR for `spec`.

    The scenario's `{label, value}` selector becomes a `labelSelectors` match inside
    `target_namespace`; every extra spec key (`containerNames`, `stressors`, `delay`,
    `direction`) passes through verbatim; `duration` is the scenario's, so Chaos Mesh
    recovers the fault on its own even if the recorder dies.
    """
    chaos = spec.chaos
    if not chaos.kind:
        raise ValueError(f"{spec.scenario_id}: no chaos block to build a manifest from")
    label = chaos.selector.get("label", "app")
    value = chaos.selector.get("value", spec.target_service)
    body: dict[str, Any] = {}
    if chaos.action:
        body["action"] = chaos.action
    body["mode"] = chaos.mode or "one"
    body["selector"] = {
        "namespaces": [spec.target_namespace],
        "labelSelectors": {label: value},
    }
    body.update(chaos.params)
    body["duration"] = f"{int(spec.duration_s)}s"
    return {
        "apiVersion": CHAOS_API_VERSION,
        "kind": chaos.kind,
        "metadata": {
            "name": name,
            "namespace": chaos_namespace,
            "labels": {"app.kubernetes.io/part-of": "inframind-eval"},
        },
        "spec": body,
    }


def config_change_commands(spec: ScenarioSpec) -> tuple[list[str], list[str]]:
    """Return `(apply, revert)` kubectl argv for a `config_change` scenario.

    Only env edits are supported (the one config scenario, test-06). The revert is
    `rollout undo`, which restores the previous ReplicaSet template exactly.
    """
    change = spec.config_change
    resource = str(change.get("resource", ""))
    env = change.get("env") or {}
    if not resource or not env:
        raise ValueError(f"{spec.scenario_id}: config_change needs `resource` and `env`")
    apply = ["-n", spec.target_namespace, "set", "env", resource]
    if change.get("container"):
        apply += ["-c", str(change["container"])]
    apply += [f"{key}={value}" for key, value in sorted(env.items())]
    revert = ["-n", spec.target_namespace, "rollout", "undo", resource]
    return apply, revert


# --- reading the bus ------------------------------------------------------------------


def _iter_stream(redis: Any, stream: str, start_ms: int, end_ms: int) -> Iterator[bytes]:
    """Yield the `data` payload of every entry with an id in `[start_ms, end_ms]`."""
    lower = f"{start_ms}-0"
    upper = f"{end_ms}-18446744073709551615"
    while True:
        page = redis.xrange(stream, min=lower, max=upper, count=_XRANGE_PAGE)
        if not page:
            return
        for _entry_id, fields in page:
            payload = fields.get(_BUS_FIELD) or fields.get(_BUS_FIELD.decode())
            if payload is not None:
                yield payload
        last = page[-1][0]
        last = last.decode() if isinstance(last, bytes) else str(last)
        ms, seq = last.split("-")
        lower = f"{ms}-{int(seq) + 1}"
        if len(page) < _XRANGE_PAGE:
            return


def read_signals(redis: Any, start: datetime, end: datetime) -> list[Signal]:
    """Signals published in `[start, end]`, sorted by `(ts, id)` as replay requires.

    Collectors publish out of `ts` order (each polls on its own clock), so the sort is
    what makes the fixture monotone. A payload that does not validate is skipped and
    counted, never guessed at.
    """
    signals: list[Signal] = []
    invalid = 0
    for payload in _iter_stream(redis, stream_keys.SIGNALS, _ms(start), _ms(end)):
        try:
            signals.append(Signal.model_validate_json(payload))
        except ValueError:
            invalid += 1
    if invalid:
        log.warning("live_record_invalid_signals", count=invalid)
    signals.sort(key=lambda s: (s.ts, s.id))
    return signals


def read_graph(redis: Any, end: datetime) -> ServiceGraph:
    """Fold every `GraphUpdate` published up to `end` into a fresh `ServiceGraph`.

    This is the graph the RCA worker would hold at `end`, rebuilt from the same stream it
    consumes (D21), so the fixture does not depend on the worker's snapshot file.
    """
    graph = ServiceGraph(seed_path=None)
    for payload in _iter_stream(redis, stream_keys.GRAPH_UPDATES, 0, _ms(end)):
        try:
            graph.apply_update(GraphUpdate.model_validate_json(payload))
        except ValueError:
            continue
    return graph


def oldest_signal_at(redis: Any) -> datetime | None:
    """When the oldest entry still in `stream:signals` was published, or None if empty."""
    page = redis.xrange(stream_keys.SIGNALS, min="-", max="+", count=1)
    if not page:
        return None
    first = page[0][0]
    first = first.decode() if isinstance(first, bytes) else str(first)
    ms = int(first.split("-")[0])
    return datetime.fromtimestamp(ms / 1000, tz=utcnow().tzinfo)


def latest_recording_end(fixture_root: Path) -> datetime | None:
    """When the newest fault under `fixture_root` was over, or None if there is none.

    A finished fixture counts by its `recorded_at`; an aborted one (its `FAULT_MARKER` is
    still there) by the marker's `ends_at`, the onset plus the fault duration and cooldown.
    """
    ends: list[datetime] = []
    for meta_path in fixture_root.glob("*/r*/meta.json"):
        try:
            recorded = FixtureMeta.from_json(meta_path).recorded_at
        except (OSError, ValueError, KeyError):
            continue
        if recorded is not None:
            ends.append(recorded)
    for marker in fixture_root.glob(f"*/r*/{FAULT_MARKER}"):
        try:
            ends.append(datetime.fromisoformat(json.loads(marker.read_text())["ends_at"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return max(ends, default=None)


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


# --- the runner -----------------------------------------------------------------------


def _subprocess_runner(argv: Sequence[str], stdin: str | None) -> tuple[int, str, str]:
    proc = subprocess.run(list(argv), input=stdin, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


class LiveRunner:
    """Records one fixture per (scenario, repeat) against the kind testbed.

    Construction resolves the kubeconfig and refuses any context that is not `kind-*`.
    `kubectl`, the Redis client, the clock and `sleep` are injectable for tests.
    """

    def __init__(
        self,
        kubeconfig_path: Path | None = None,
        *,
        params: EvalParams | None = None,
        redis: Any = None,
        run_command: CommandRunner | None = None,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._params = params or EvalParams()
        self._run = run_command or _subprocess_runner
        if run_command is None and shutil.which("kubectl") is None:
            raise WrongKubeContext("kubectl not on PATH; refusing to run live scenarios")
        self._kubeconfig = Path(kubeconfig_path) if kubeconfig_path else self._resolve_kubeconfig()
        self._context = self._parse_context()
        self._redis = redis
        self._now = now
        self._sleep = sleep

    @property
    def context(self) -> str:
        """The active cluster context. Always starts with `kind-` or construction raises."""
        return self._context

    def _resolve_kubeconfig(self) -> Path:
        """Pick the kubeconfig the testbed owns, never `~/.kube/config`."""
        explicit = os.environ.get(KUBECONFIG_ENV, "").strip()
        if explicit:
            return Path(explicit)
        repo_local = Path("testbed/.kube/config")
        if repo_local.exists():
            return repo_local.resolve()
        raise WrongKubeContext(
            "no KUBECONFIG and testbed/.kube/config missing; refuse to touch ~/.kube/config"
        )

    def _parse_context(self) -> str:
        """Read the current-context from kubeconfig; raise unless it starts with `kind-`."""
        code, out, err = self._run(
            ["kubectl", f"--kubeconfig={self._kubeconfig}", "config", "current-context"], None
        )
        if code != 0:
            raise WrongKubeContext(f"kubectl config current-context failed: {err.strip()}")
        context = out.strip()
        if not context.startswith(KIND_CONTEXT_PREFIX):
            raise WrongKubeContext(
                f"context {context!r} is not a kind cluster (must start with {KIND_CONTEXT_PREFIX!r})"
            )
        return context

    def kubectl(self, args: Sequence[str], *, stdin: str | None = None) -> str:
        """Run one kubectl command pinned to this kubeconfig and context; raise on failure."""
        argv = [
            "kubectl",
            f"--kubeconfig={self._kubeconfig}",
            f"--context={self._context}",
            *args,
        ]
        code, out, err = self._run(argv, stdin)
        if code != 0:
            raise LiveRunnerError(f"{' '.join(args[:4])} failed: {err.strip()}")
        return out

    def _redis_client(self) -> Any:
        if self._redis is None:
            from redis import Redis

            self._redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        return self._redis

    def run_scenario(
        self,
        spec: ScenarioSpec,
        *,
        repeat: int,
        record_to: Path,
        allow_blocked: bool = False,
        overwrite: bool = False,
    ) -> Recording:
        """Inject `spec`'s fault once and write the fixture to `record_to` (module docstring).

        Raises:
            BlockedScenarioError: `spec.is_blocked` and `allow_blocked` is False.
            LiveRunnerError: `record_to` already holds a fixture and `overwrite` is False,
                or a kubectl call failed.
        """
        if spec.is_blocked and not allow_blocked:
            raise BlockedScenarioError(
                f"{spec.scenario_id} is blocked ({spec.block_reason or BLOCKED_REASON}); "
                "pass --allow-blocked to record it anyway."
            )
        if (record_to / "signals.ndjson").exists() and not overwrite:
            raise LiveRunnerError(f"{record_to} already holds a fixture; refusing to overwrite")

        redis = self._redis_client()
        pre_s = max(spec.warmup_s, self._params.baseline_s)
        self._wait_for_quiet(pre_s)
        self._wait_for_baseline(redis, pre_s)

        onset = self._now()
        log.info("live_fault_inject", scenario_id=spec.scenario_id, repeat=repeat)
        self._mark_fault(record_to, onset, spec)
        revert = self._inject(spec, repeat)
        try:
            self._sleep(spec.duration_s)
        finally:
            revert()
            log.info("live_fault_reverted", scenario_id=spec.scenario_id, repeat=repeat)

        self._sleep(spec.cooldown_s + self._params.settle_s)
        end = self._now()

        signals = read_signals(redis, onset - timedelta(seconds=pre_s), end)
        graph = read_graph(redis, end)
        meta = FixtureMeta(
            scenario_id=spec.scenario_id,
            seed=spec.seed,
            repeat=repeat,
            onset_ts=onset,
            window_s=spec.duration_s + spec.cooldown_s,
            recorded_at=end,
        )
        self._write(record_to, signals, graph, meta, pre_s=pre_s)
        edges = len(graph.snapshot()["edges"])
        log.info(
            "live_fixture_recorded",
            scenario_id=spec.scenario_id,
            repeat=repeat,
            fixture_dir=str(record_to),
            signals=len(signals),
            edges=edges,
        )
        return Recording(record_to, meta, len(signals), edges)

    def _wait_for_quiet(self, pre_s: float) -> None:
        """Sleep until the newest recording ended at least `pre_s` ago (module docstring)."""
        last = latest_recording_end(Path(self._params.fixture_dir))
        if last is None:
            return
        missing = pre_s - (self._now() - last).total_seconds()
        if missing > 0:
            log.info("live_waiting_for_quiet_baseline", seconds=round(missing, 1))
            self._sleep(missing)

    def _wait_for_baseline(self, redis: Any, pre_s: float) -> None:
        """Sleep until `stream:signals` holds `pre_s` seconds of history before the onset."""
        oldest = oldest_signal_at(redis)
        if oldest is None:
            raise LiveRunnerError("stream:signals is empty; start the collectors (`make up`)")
        missing = pre_s - (self._now() - oldest).total_seconds()
        if missing > 0:
            log.info("live_waiting_for_baseline", seconds=round(missing, 1))
            self._sleep(missing)

    def _inject(self, spec: ScenarioSpec, repeat: int) -> Callable[[], None]:
        """Apply the fault now and return the callable that reverts it."""
        if spec.chaos.kind:
            name = chaos_resource_name(spec, repeat)
            namespace = self._params.chaos_namespace
            manifest = build_chaos_manifest(spec, name=name, chaos_namespace=namespace)
            self.kubectl(["apply", "-f", "-"], stdin=json.dumps(manifest))
            kind = spec.chaos.kind.lower()

            def revert_chaos() -> None:
                self.kubectl(["-n", namespace, "delete", kind, name, "--ignore-not-found"])

            return revert_chaos

        apply, undo = config_change_commands(spec)
        self.kubectl(apply)

        def revert_config() -> None:
            self.kubectl(undo)

        return revert_config

    @staticmethod
    def _write(
        record_to: Path,
        signals: Sequence[Signal],
        graph: ServiceGraph,
        meta: FixtureMeta,
        *,
        pre_s: float,
    ) -> None:
        """Write `signals.ndjson`, `graph.json`, `meta.json` (meta last: it marks completion)."""
        if not signals:
            raise LiveRunnerError(f"{meta.scenario_id}: no signals in the recorded window")
        record_to.mkdir(parents=True, exist_ok=True)
        lines = "".join(s.model_dump_json() + "\n" for s in signals)
        (record_to / "signals.ndjson").write_text(lines)
        (record_to / "graph.json").write_text(json.dumps(graph.snapshot(), indent=2) + "\n")
        provenance = {**meta.as_dict(), "baseline_s": pre_s, "recorded_live": True}
        (record_to / "meta.json").write_text(json.dumps(provenance, indent=2) + "\n")
        (record_to / FAULT_MARKER).unlink(missing_ok=True)

    @staticmethod
    def _mark_fault(record_to: Path, onset: datetime, spec: ScenarioSpec) -> None:
        """Write `FAULT_MARKER` before injecting (removed by `_write` on success)."""
        record_to.mkdir(parents=True, exist_ok=True)
        ends_at = onset + timedelta(seconds=spec.duration_s + spec.cooldown_s)
        marker = {"onset_ts": onset.isoformat(), "ends_at": ends_at.isoformat()}
        (record_to / FAULT_MARKER).write_text(json.dumps(marker) + "\n")


def list_blocked_reasons(scenarios: Sequence[ScenarioSpec]) -> dict[str, str]:
    """Return `{scenario_id: block_reason}` for every blocked scenario in `scenarios`."""
    return {spec.scenario_id: spec.block_reason for spec in scenarios if spec.is_blocked}
