"""Live runner: injects one scenario's fault on the kind testbed and records a fixture.

This module is the **only** place in `evaluation/` that calls `kubectl`. A live run records;
it never scores. Scoring always replays the recorded fixture (D7-Replay), so a number in
the paper is reproducible from `evaluation/fixtures/<scenario>/r<repeat>/` alone.

One recording (`LiveRunner.run_scenario`):

1. **Baseline.** Wait until `stream:signals` holds at least `pre_s` seconds of unbroken
   history (no gap over `baseline_max_gap_s`, so entries from before a restart don't count)
   (`max(spec.warmup_s, EvalParams.baseline_s)`; the detectors need 10 samples and the
   Prometheus collector polls every 30 s, so the default is 600 s), **and** until the end
   of the newest recording under `fixture_dir` is at least `pre_s` in the past, so no
   earlier fault (another repeat or scenario) lies inside this run's baseline window.
   Back-to-back repeats 287 s apart once put the previous kill inside the next baseline:
   the incident opened before the fault and every later repeat scored a false 0 s
   latency.
2. **Inject** at `onset_ts`: apply the Chaos Mesh CR built from `spec.chaos`
   (`build_chaos_manifest`), or apply `spec.config_change` with `kubectl set env`, then
   **confirm it took effect**: the CR must reach `AllInjected=True` (a `Failed` record aborts
   at once), or the Deployment must show a new, observed generation. An accepted apply is
   not proof: on this kind runtime every `container-kill` was accepted and then failed
   (`expected docker:// but got containerd://`), and five fault-free periods were recorded
   as dev-02 before this check existed.
3. **Hold** for `duration_s`, then **revert** (delete the CR / `kubectl rollout undo`). The
   revert runs in a `finally`, and every CR also carries a Chaos Mesh `duration`, so a
   crashed recorder cannot leave a fault behind.
4. **Settle** for `cooldown_s + settle_s` so the collectors publish the recovery.
5. **Record** `stream:signals` entries from `onset - pre_s` to the end, sorted by `ts`,
   the service graph folded from every `stream:graph-updates` entry up to the end, the
   K8s change events the RCA process's watcher stored from `onset - pre_s - 1800 s` to the
   end (`changes.json`, read from the API's `GET /change-events`, D22), and a `meta.json`
   with the onset and window. The API is checked before the fault, so a recording never
   silently lacks its change log.

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
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from inframind.common import stream_keys
from inframind.common.graph_update import GraphUpdate
from inframind.common.logging import get_logger
from inframind.common.signal import Signal
from inframind.common.time import utcnow
from inframind.rca.change_correlation import ChangeEvent
from inframind.rca.graph import ServiceGraph

from ..config import EvalParams
from ..scenarios import BLOCKED_REASON
from ..schema import FixtureMeta, ScenarioSpec
from .replay import CHANGES_JSON, SIGNALS_GZ, has_signals, write_signals_gz

log = get_logger(__name__)

KIND_CONTEXT_PREFIX = "kind-"
KUBECONFIG_ENV = "KUBECONFIG"
CHAOS_API_VERSION = "chaos-mesh.org/v1alpha1"

#: Written into a repeat's directory just before its fault is applied and removed once the
#: fixture is complete, so a recorder killed mid-fault still counts that fault in the next
#: run's quiet-baseline wait (`latest_recording_end`).
FAULT_MARKER = "fault-started.json"

#: How far before the baseline the recorder copies change events: the ranker's change
#: correlation pre-window (`CHANGE_CORRELATION_PRE_WINDOW_S`, standard 14.7).
CHANGE_LOOKBACK_S = 1800.0

#: `(start, end) -> events`: reads the stored change log (the API by default).
ChangeFetcher = Callable[[datetime, datetime], list[ChangeEvent]]

#: The bus stores each model as JSON under this field (`common/bus.py`).
_BUS_FIELD = b"data"
_XRANGE_PAGE = 5_000

#: `run(argv, stdin) -> (returncode, stdout, stderr)`; injectable so tests never shell out.
CommandRunner = Callable[[Sequence[str], str | None], tuple[int, str, str]]

__all__ = [
    "CHANGES_JSON",
    "FAULT_MARKER",
    "KIND_CONTEXT_PREFIX",
    "BlockedScenarioError",
    "LiveRunner",
    "LiveRunnerError",
    "Recording",
    "WrongKubeContext",
    "build_chaos_manifest",
    "chaos_injection_state",
    "chaos_resource_name",
    "config_change_commands",
    "fetch_changes_from_api",
    "fresh_history_start",
    "latest_recording_end",
    "list_blocked_reasons",
    "read_graph",
    "read_signals",
    "rollout_state",
    "write_changes",
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
    """Signals observed in `[start, end]`, sorted by `(ts, id)` as replay requires.

    The stream range is by publish time, so a late-published line (Loki can lag by tens of
    seconds) whose `ts` falls before `start` is dropped: on the live dev-01 r3 recording,
    error lines from a kill 4 minutes before the window slipped into its baseline that way.
    Collectors publish out of `ts` order, so the sort is what makes the fixture monotone.
    A payload that does not validate is skipped and counted, never guessed at.
    """
    signals: list[Signal] = []
    invalid = 0
    for payload in _iter_stream(redis, stream_keys.SIGNALS, _ms(start), _ms(end)):
        try:
            signal = Signal.model_validate_json(payload)
        except ValueError:
            invalid += 1
            continue
        if start <= signal.ts <= end:
            signals.append(signal)
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


def fetch_changes_from_api(api_url: str) -> ChangeFetcher:
    """A `ChangeFetcher` over the InfraMind API's `GET /change-events`."""

    def fetch(start: datetime, end: datetime) -> list[ChangeEvent]:
        import httpx

        try:
            response = httpx.get(
                f"{api_url.rstrip('/')}/change-events",
                params={"start": start.isoformat(), "end": end.isoformat()},
                timeout=10.0,
            )
            response.raise_for_status()
            items = response.json()["items"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise LiveRunnerError(f"could not read change events from {api_url}: {e}") from e
        return [ChangeEvent.model_validate(item) for item in items]

    return fetch


def write_changes(path: Path, events: Sequence[ChangeEvent]) -> None:
    """Write `changes.json`: the events sorted by `(occurred_at, resource)`."""
    ordered = sorted(events, key=lambda e: (e.occurred_at, e.resource))
    payload = {"events": [e.model_dump(mode="json") for e in ordered]}
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _entry_ms(entry_id: Any) -> int:
    """Milliseconds part of a Redis stream entry id (`<ms>-<seq>`)."""
    text = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
    return int(text.split("-")[0])


def fresh_history_start(
    redis: Any, now: datetime, *, lookback_s: float, max_gap_s: float
) -> datetime | None:
    """Start of the unbroken run of `stream:signals` entries that reaches up to `now`.

    Walks entries published in `[now - lookback_s - max_gap_s, now]` from newest to oldest
    and stops at the first gap longer than `max_gap_s`. Entries left over from before a
    restart are separated from fresh ones by such a gap, so they never count as baseline
    (reading only the oldest entry counted them: dev-03, 2026-11-17).

    Returns None when no entry was published in the last `max_gap_s` (collectors down).
    The result is never earlier than the window start, so a caller only learns whether at
    least `lookback_s` of unbroken history exists, not how much more.
    """
    now_ms = int(now.timestamp() * 1000)
    floor_ms = now_ms - int((lookback_s + max_gap_s) * 1000)
    gap_ms = max_gap_s * 1000
    newest = now_ms
    cursor: str = str(now_ms)
    seen_any = False
    while True:
        page = redis.xrevrange(
            stream_keys.SIGNALS, max=cursor, min=str(floor_ms), count=_XRANGE_PAGE
        )
        if not page:
            break
        for entry_id, _ in page:
            ms = _entry_ms(entry_id)
            if newest - ms > gap_ms:
                return None if not seen_any else _from_ms(newest)
            newest = ms
            seen_any = True
        if len(page) < _XRANGE_PAGE:
            break
        last = page[-1][0]
        last = last.decode() if isinstance(last, bytes) else str(last)
        cursor = "(" + last
    if not seen_any:
        return None
    return _from_ms(newest)


def _from_ms(ms: int) -> datetime:
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


def chaos_injection_state(obj: Mapping[str, Any]) -> tuple[bool | None, str]:
    """Read a Chaos Mesh CR: `(True, "")` injected, `(False, why)` failed, `(None, ...)` pending.

    `AllInjected=True` is success. A record with a `Failed` event and not injected is a
    definite failure (its message names the cause, e.g. the container-runtime mismatch).
    """
    status = obj.get("status") or {}
    conditions = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
    if conditions.get("AllInjected") == "True":
        return True, ""
    for record in (status.get("experiment") or {}).get("containerRecords") or []:
        failed = [e for e in record.get("events") or [] if e.get("type") == "Failed"]
        if failed and record.get("phase") != "Injected":
            return False, str(failed[-1].get("message") or "Failed")[:300]
    return None, "waiting for AllInjected"


def rollout_state(obj: Mapping[str, Any], before: int) -> tuple[bool | None, str]:
    """A Deployment edit took effect once only new pods are left to serve traffic.

    A new, observed generation is not enough: with a rolling update, a new pod that never
    becomes Ready leaves the old pod serving and the fault never reaches traffic. So the
    updated replicas must equal the desired count and be the only replicas left.
    """
    generation = int((obj.get("metadata") or {}).get("generation", 0))
    status = obj.get("status") or {}
    observed = int(status.get("observedGeneration", 0))
    desired = int((obj.get("spec") or {}).get("replicas", 1))
    updated = int(status.get("updatedReplicas", 0))
    total = int(status.get("replicas", 0))
    if generation > before and observed >= generation and updated == desired == total:
        return True, ""
    return None, (
        f"generation {generation} (before {before}), observed {observed}, "
        f"updated {updated}/{desired}, total {total}"
    )


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
        fetch_changes: ChangeFetcher | None = None,
    ) -> None:
        self._params = params or EvalParams()
        self._fetch_changes = fetch_changes or fetch_changes_from_api(self._params.api_url)
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
        if has_signals(record_to) and not overwrite:
            raise LiveRunnerError(f"{record_to} already holds a fixture; refusing to overwrite")

        redis = self._redis_client()
        pre_s = max(spec.warmup_s, self._params.baseline_s)
        self._wait_for_quiet(pre_s)
        self._wait_for_baseline(redis, pre_s)
        # Fail before the fault, not after it, when the change log cannot be read.
        probe_end = self._now()
        self._fetch_changes(probe_end - timedelta(seconds=60), probe_end)

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
        changes = self._fetch_changes(onset - timedelta(seconds=pre_s + CHANGE_LOOKBACK_S), end)
        self._check_change_observed(spec, changes, onset, end)
        meta = FixtureMeta(
            scenario_id=spec.scenario_id,
            seed=spec.seed,
            repeat=repeat,
            onset_ts=onset,
            window_s=spec.duration_s + spec.cooldown_s,
            recorded_at=end,
        )
        self._write(record_to, signals, graph, meta, pre_s=pre_s, changes=changes)
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

    @staticmethod
    def _check_change_observed(
        spec: ScenarioSpec, changes: Sequence[ChangeEvent], onset: datetime, end: datetime
    ) -> None:
        """Warn when a config-change fault left no change event on its target service.

        Not an error: the fixture records what the live watcher saw, and a miss is part of
        that. But it usually means the watcher is not running (`change_watcher_disabled`).
        """
        if not spec.config_change:
            return
        # The changed Deployment's name, e.g. "deployment/frontend" -> "frontend".
        target = str(spec.config_change.get("resource", "")).rsplit("/", 1)[-1]
        seen = any(
            e.service == target and onset - timedelta(seconds=60) <= e.occurred_at <= end
            for e in changes
        )
        if not seen:
            log.warning(
                "live_change_not_observed",
                scenario_id=spec.scenario_id,
                service=target,
                hint="is the rca container's change watcher running? (docker logs inframind-rca)",
            )

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
        """Sleep until `stream:signals` holds `pre_s` seconds of unbroken, fresh history.

        Re-checks after every sleep, so a collector outage during the wait restarts it.

        Raises:
            LiveRunnerError: nothing was published in the last `baseline_max_gap_s`.
        """
        max_gap_s = self._params.baseline_max_gap_s
        while True:
            now = self._now()
            start = fresh_history_start(redis, now, lookback_s=pre_s, max_gap_s=max_gap_s)
            if start is None:
                raise LiveRunnerError(
                    f"no signal published in the last {max_gap_s:.0f} s; "
                    "start the collectors (`make up`)"
                )
            missing = pre_s - (now - start).total_seconds()
            if missing <= 0:
                return
            log.info("live_waiting_for_baseline", seconds=round(missing, 1))
            self._sleep(missing)

    def _inject(self, spec: ScenarioSpec, repeat: int) -> Callable[[], None]:
        """Apply the fault, confirm it took effect, and return the callable that reverts it.

        Raises:
            LiveRunnerError: the fault could not be confirmed; it has been reverted.
        """
        if spec.chaos.kind:
            name = chaos_resource_name(spec, repeat)
            namespace = self._params.chaos_namespace
            manifest = build_chaos_manifest(spec, name=name, chaos_namespace=namespace)
            kind = spec.chaos.kind.lower()

            def revert_chaos() -> None:
                self.kubectl(["-n", namespace, "delete", kind, name, "--ignore-not-found"])

            self.kubectl(["apply", "-f", "-"], stdin=json.dumps(manifest))
            self._confirm_or_revert(
                lambda: chaos_injection_state(
                    self._get_json(["-n", namespace, "get", kind, name, "-o", "json"])
                ),
                revert_chaos,
                f"{kind}/{name}",
            )
            return revert_chaos

        apply, undo = config_change_commands(spec)
        resource = str(spec.config_change["resource"])
        get = ["-n", spec.target_namespace, "get", resource, "-o", "json"]
        before = int(self._get_json(get).get("metadata", {}).get("generation", 0))

        def revert_config() -> None:
            self.kubectl(undo)

        self.kubectl(apply)
        self._confirm_or_revert(
            lambda: rollout_state(self._get_json(get), before), revert_config, resource
        )
        return revert_config

    def _get_json(self, args: Sequence[str]) -> dict[str, Any]:
        out = self.kubectl(args)
        try:
            loaded = json.loads(out) if out.strip() else {}
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _confirm_or_revert(
        self,
        check: Callable[[], tuple[bool | None, str]],
        revert: Callable[[], None],
        what: str,
    ) -> None:
        """Poll `check` until it confirms the fault; on failure or timeout, revert and raise."""
        waited = 0.0
        while True:
            verdict, detail = check()
            if verdict is True:
                log.info("live_fault_confirmed", resource=what, after_s=waited)
                return
            if verdict is False or waited >= self._params.inject_timeout_s:
                revert()
                reason = detail if verdict is False else f"not confirmed in {waited:.0f}s"
                raise LiveRunnerError(f"{what}: fault did not take effect ({reason})")
            self._sleep(self._params.inject_poll_s)
            waited += self._params.inject_poll_s

    @staticmethod
    def _write(
        record_to: Path,
        signals: Sequence[Signal],
        graph: ServiceGraph,
        meta: FixtureMeta,
        *,
        pre_s: float,
        changes: Sequence[ChangeEvent] = (),
    ) -> None:
        """Write the signals, graph, `changes.json` and `meta.json` (meta last: completion)."""
        if not signals:
            raise LiveRunnerError(f"{meta.scenario_id}: no signals in the recorded window")
        record_to.mkdir(parents=True, exist_ok=True)
        write_signals_gz(record_to / SIGNALS_GZ, signals)
        (record_to / "graph.json").write_text(json.dumps(graph.snapshot(), indent=2) + "\n")
        write_changes(record_to / CHANGES_JSON, changes)
        provenance = {
            **meta.as_dict(),
            "baseline_s": pre_s,
            "recorded_live": True,
            "changes_recorded": True,
            "change_events": len(changes),
        }
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
