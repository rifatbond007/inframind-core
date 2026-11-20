"""Live recorder tests: fake kubectl, fakeredis and a fake clock; no cluster is touched."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from inframind.common import stream_keys
from inframind.common.graph_update import GraphUpdate
from inframind.common.ids import new_ulid
from inframind.common.signal import Severity, Signal, SignalType, Source
from inframind.evaluation.config import EvalParams
from inframind.evaluation.runner.live import (
    FAULT_MARKER,
    BlockedScenarioError,
    LiveRunner,
    LiveRunnerError,
    WrongKubeContext,
    build_chaos_manifest,
    chaos_resource_name,
    config_change_commands,
    fresh_history_start,
    read_signals,
)
from inframind.evaluation.runner.replay import replay_signals, signals_file, write_signals_gz
from inframind.evaluation.scenarios import load_set
from inframind.evaluation.schema import FixtureMeta, ScenarioSpec

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _spec(scenario_id: str) -> ScenarioSpec:
    """A committed scenario by id (dev or test)."""
    for spec in load_set("dev") + load_set("test"):
        if spec.scenario_id == scenario_id:
            return spec
    raise KeyError(scenario_id)


#: Status of a real PodChaos on this kind cluster, captured 2026-11-16 (pod-failure probe).
INJECTED = {
    "status": {
        "conditions": [
            {"type": "Selected", "status": "True"},
            {"type": "AllInjected", "status": "True"},
            {"type": "AllRecovered", "status": "False"},
        ],
        "experiment": {
            "containerRecords": [
                {
                    "id": "default/currencyservice-x",
                    "phase": "Injected",
                    "injectedCount": 1,
                    "events": [{"type": "Succeeded", "operation": "Apply"}],
                }
            ]
        },
    }
}

#: The real container-kill status on the same cluster: the runtime refused it (D26).
RUNTIME_REFUSED = {
    "status": {
        "conditions": [
            {"type": "Selected", "status": "True"},
            {"type": "AllInjected", "status": "False"},
            {"type": "AllRecovered", "status": "True"},
        ],
        "experiment": {
            "containerRecords": [
                {
                    "id": "default/shippingservice-x/server",
                    "phase": "Not Injected",
                    "injectedCount": 0,
                    "events": [
                        {
                            "type": "Failed",
                            "operation": "Apply",
                            "message": "rpc error: code = Unknown desc = expected docker:// "
                            "but got containerd://abc",
                        }
                    ],
                }
            ]
        },
    }
}


class FakeKubectl:
    """Records every argv; answers `current-context`, and `get -o json` with a status.

    `chaos` is the JSON a `get <chaos kind>` returns (default: injected). `deploy` is the
    sequence of `(generation, observedGeneration)` successive Deployment `get`s return.
    """

    def __init__(
        self,
        context: str = "kind-inframind",
        fail_on: str | None = None,
        chaos: dict | None = None,
        deploy: list[tuple[int, int]] | None = None,
    ) -> None:
        self.context = context
        self.fail_on = fail_on
        self.chaos = INJECTED if chaos is None else chaos
        self.deploy = list(deploy or [(3, 3), (4, 4)])
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv: Sequence[str], stdin: str | None) -> tuple[int, str, str]:
        argv = list(argv)
        self.calls.append((argv, stdin))
        if "current-context" in argv:
            return 0, self.context + "\n", ""
        if self.fail_on and self.fail_on in argv:
            return 1, "", "boom"
        if "get" in argv and "json" in argv:
            if any(a.startswith("deployment/") for a in argv):
                gen, observed = self.deploy.pop(0) if len(self.deploy) > 1 else self.deploy[0]
                body = {
                    "metadata": {"generation": gen},
                    "spec": {"replicas": 1},
                    "status": {"observedGeneration": observed, "updatedReplicas": 1, "replicas": 1},
                }
                return 0, json.dumps(body), ""
            return 0, json.dumps(self.chaos), ""
        return 0, "", ""

    def verbs(self) -> list[str]:
        """The kubectl verb of each call after the context check."""
        out = []
        for argv, _ in self.calls[1:]:
            rest = [a for a in argv[1:] if not a.startswith("--")]
            rest = rest[2:] if rest[:1] == ["-n"] else rest
            out.append(rest[0])
        return out


class FakeClock:
    """`now()` and `sleep()` that move together, so a 10-minute run takes no time."""

    def __init__(self, start: datetime) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


def _signal(ts: datetime, service: str = "cartservice") -> Signal:
    return Signal(
        id=new_ulid(),
        ts=ts,
        source=Source.PROMETHEUS,
        type=SignalType.METRIC,
        service=service,
        severity=Severity.INFO,
        attrs={"metric": "cpu_cores", "value": 0.01},
    )


def _xadd(redis: Any, stream: str, at: datetime, payload: str, seq: int = 0) -> None:
    redis.xadd(stream, {"data": payload}, id=f"{int(at.timestamp() * 1000)}-{seq}")


def _seed_bus(redis: Any, *, history_s: int, run_s: int) -> None:
    """Publish one signal every 30 s from `T0 - history_s` to `T0 + run_s`.

    Each entry's `ts` lags its publish time by a varying amount, the way collectors do, so
    publish order is not `ts` order.
    """
    for i, offset in enumerate(range(-history_s, run_s, 30)):
        at = T0 + timedelta(seconds=offset)
        ts = at - timedelta(seconds=(i % 3) * 20)
        _xadd(redis, stream_keys.SIGNALS, at, _signal(ts).model_dump_json())
    update = GraphUpdate(
        caller="checkoutservice", callee="cartservice", latency_ms=3.0, observed_at=T0
    )
    for seq in range(3):
        _xadd(redis, stream_keys.GRAPH_UPDATES, T0, update.model_dump_json(), seq)


def _runner(
    redis: Any, clock: FakeClock, kubectl: FakeKubectl, tmp_path: Path, **params: Any
) -> LiveRunner:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("")
    return LiveRunner(
        kubeconfig,
        params=EvalParams(
            **{
                "baseline_s": 600.0,
                "settle_s": 45.0,
                "fixture_dir": str(tmp_path / "fixtures"),
                **params,
            }
        ),
        redis=redis,
        run_command=kubectl,
        now=clock.now,
        sleep=clock.sleep,
    )


# --- guards -----------------------------------------------------------------------------


def test_refuses_a_context_that_is_not_kind(tmp_path: Path) -> None:
    """Every kubectl call is pinned to a kind-* context; anything else is refused."""
    with pytest.raises(WrongKubeContext, match="not a kind cluster"):
        _runner(None, FakeClock(T0), FakeKubectl("prod-cluster"), tmp_path)


def test_refuses_a_blocked_scenario(tmp_path: Path) -> None:
    runner = _runner(fakeredis.FakeRedis(), FakeClock(T0), FakeKubectl(), tmp_path)
    with pytest.raises(BlockedScenarioError):
        runner.run_scenario(
            _spec("dev-stresschaos-04-cartservice-cpu-stress"), repeat=0, record_to=tmp_path
        )


def test_refuses_to_overwrite_a_recording(tmp_path: Path) -> None:
    (tmp_path / "signals.ndjson").write_text("{}\n")
    runner = _runner(fakeredis.FakeRedis(), FakeClock(T0), FakeKubectl(), tmp_path)
    with pytest.raises(LiveRunnerError, match="refusing to overwrite"):
        runner.run_scenario(
            _spec("dev-podchaos-01-cartservice-pod-kill"), repeat=0, record_to=tmp_path
        )


# --- pure builders ----------------------------------------------------------------------


def test_chaos_manifest_for_container_kill() -> None:
    """Selector, containerNames and a self-expiring duration all reach the CR."""
    spec = _spec("dev-podchaos-02-shippingservice-container-kill")
    cr = build_chaos_manifest(spec, name="n")
    assert cr["apiVersion"] == "chaos-mesh.org/v1alpha1"
    assert cr["kind"] == "PodChaos"
    assert cr["metadata"] == {
        "name": "n",
        "namespace": "chaos-testing",
        "labels": {"app.kubernetes.io/part-of": "inframind-eval"},
    }
    assert cr["spec"]["action"] == "container-kill"
    assert cr["spec"]["containerNames"] == ["server"]
    assert cr["spec"]["selector"] == {
        "namespaces": ["default"],
        "labelSelectors": {"app": "shippingservice"},
    }
    assert cr["spec"]["duration"] == "180s"


def test_chaos_manifest_for_stress_has_no_action() -> None:
    """StressChaos is defined by its stressors; an `action` key fails CRD validation."""
    cr = build_chaos_manifest(_spec("dev-stresschaos-04-cartservice-cpu-stress"), name="n")
    assert "action" not in cr["spec"]
    assert cr["spec"]["stressors"] == {"cpu": {"workers": 2, "load": 80}}


def test_chaos_resource_name_is_dns_safe_and_bounded() -> None:
    spec = _spec("test-podchaos-03-paymentservice-pod-failure")
    name = chaos_resource_name(spec, 4)
    assert name == "eval-test-podchaos-03-paymentservice-pod-failure-r4"
    long = ScenarioSpec(**{**_as_kwargs(spec), "scenario_id": "x" * 80})
    assert len(chaos_resource_name(long, 4)) <= 63
    assert chaos_resource_name(long, 4).endswith("-r4")


def _as_kwargs(spec: ScenarioSpec) -> dict[str, Any]:
    return {f: getattr(spec, f) for f in spec.__dataclass_fields__}


def test_config_change_commands_set_env_and_rollout_undo() -> None:
    apply, revert = config_change_commands(_spec("test-config-06-cartservice-bad-redis-addr"))
    assert apply == [
        "-n",
        "default",
        "set",
        "env",
        "deployment/cartservice",
        "-c",
        "server",
        "REDIS_ADDR=redis-cart:6380",
    ]
    assert revert == ["-n", "default", "rollout", "undo", "deployment/cartservice"]


# --- a full recording -------------------------------------------------------------------


def test_run_scenario_records_a_replayable_fixture(tmp_path: Path) -> None:
    """Inject, hold, revert, settle, record: the fixture replays and carries provenance."""
    redis = fakeredis.FakeRedis()
    _seed_bus(redis, history_s=900, run_s=600)
    clock, kubectl = FakeClock(T0), FakeKubectl()
    spec = _spec("dev-podchaos-01-cartservice-pod-kill")
    target = tmp_path / spec.scenario_id / "r2"

    recording = _runner(redis, clock, kubectl, tmp_path).run_scenario(
        spec, repeat=2, record_to=target
    )

    # The CR was applied as JSON on stdin, then deleted by name after the fault duration.
    assert kubectl.verbs() == ["apply", "get", "delete"]
    applied = json.loads(kubectl.calls[1][1] or "")
    assert applied["metadata"]["name"] == "eval-dev-podchaos-01-cartservice-pod-kill-r2"
    assert all("--context=kind-inframind" in argv for argv, _ in kubectl.calls[1:])
    # History was already long enough: no baseline wait; hold the fault, then settle.
    assert clock.slept == [spec.duration_s, spec.cooldown_s + 45.0]

    assert (target / "signals.ndjson.gz").exists()  # live recordings are committed compressed
    signals = replay_signals(signals_file(target))  # strict: monotone ts, valid lines
    assert len(signals) == recording.signal_count > 0
    end = T0 + timedelta(seconds=spec.duration_s + spec.cooldown_s + 45.0)
    assert signals[0].ts >= T0 - timedelta(seconds=600 + 60)
    assert signals[-1].ts <= end

    meta = FixtureMeta.from_json(target / "meta.json")
    assert (meta.scenario_id, meta.seed, meta.repeat) == (spec.scenario_id, spec.seed, 2)
    assert meta.onset_ts == T0
    assert meta.window_s == spec.duration_s + spec.cooldown_s
    assert json.loads((target / "meta.json").read_text())["recorded_live"] is True

    graph = json.loads((target / "graph.json").read_text())
    assert [(e["caller"], e["callee"], e["observations"]) for e in graph["edges"]] == [
        ("checkoutservice", "cartservice", 3)
    ]


def test_run_scenario_waits_for_a_full_baseline(tmp_path: Path) -> None:
    """With only 100 s of history it sleeps the missing 500 s before injecting."""
    redis = fakeredis.FakeRedis()
    _seed_bus(redis, history_s=100, run_s=1500)
    clock = FakeClock(T0)
    spec = _spec("dev-podchaos-01-cartservice-pod-kill")
    recording = _runner(redis, clock, FakeKubectl(), tmp_path).run_scenario(
        spec, repeat=0, record_to=tmp_path / "r0"
    )
    assert clock.slept[0] == pytest.approx(500.0)
    assert recording.meta.onset_ts == T0 + timedelta(seconds=500)


def test_run_scenario_reverts_the_fault_when_interrupted(tmp_path: Path) -> None:
    """A crash mid-fault still deletes the CR (the revert is in a `finally`)."""
    redis = fakeredis.FakeRedis()
    _seed_bus(redis, history_s=900, run_s=60)
    kubectl = FakeKubectl()

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    runner = _runner(redis, FakeClock(T0), kubectl, tmp_path)
    runner._sleep = interrupted
    with pytest.raises(KeyboardInterrupt):
        runner.run_scenario(
            _spec("dev-podchaos-01-cartservice-pod-kill"), repeat=0, record_to=tmp_path / "r0"
        )
    assert kubectl.verbs() == ["apply", "get", "delete"]
    assert sorted(p.name for p in (tmp_path / "r0").iterdir()) == [FAULT_MARKER]  # no fixture


def test_run_scenario_applies_and_undoes_a_config_change(tmp_path: Path) -> None:
    redis = fakeredis.FakeRedis()
    _seed_bus(redis, history_s=900, run_s=600)
    kubectl = FakeKubectl()
    _runner(redis, FakeClock(T0), kubectl, tmp_path).run_scenario(
        _spec("test-config-06-cartservice-bad-redis-addr"), repeat=0, record_to=tmp_path / "r0"
    )
    assert kubectl.verbs() == ["get", "set", "get", "rollout"]


def test_run_scenario_refuses_an_empty_bus(tmp_path: Path) -> None:
    runner = _runner(fakeredis.FakeRedis(), FakeClock(T0), FakeKubectl(), tmp_path)
    with pytest.raises(LiveRunnerError, match="start the collectors"):
        runner.run_scenario(
            _spec("dev-podchaos-01-cartservice-pod-kill"), repeat=0, record_to=tmp_path / "r0"
        )


def test_read_signals_pages_through_a_long_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """More entries than one XRANGE page are all read, in ts order."""
    import inframind.evaluation.runner.live as live

    monkeypatch.setattr(live, "_XRANGE_PAGE", 7)
    redis = fakeredis.FakeRedis()
    for i in range(30):
        at = T0 + timedelta(seconds=i)
        _xadd(redis, stream_keys.SIGNALS, at, _signal(at).model_dump_json())
    signals = read_signals(redis, T0, T0 + timedelta(seconds=29))
    assert len(signals) == 30
    assert [s.ts for s in signals] == sorted(s.ts for s in signals)


# --- the CLI ------------------------------------------------------------------------------


def test_main_live_records_every_repeat_and_skips_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--live` asks for one recording per repeat, in r<k> dirs, and never scores."""
    import inframind.evaluation.runner.live as live
    from inframind.evaluation.runner import main as main_mod

    calls: list[tuple[str, int, Path]] = []

    class StubRunner:
        def __init__(self, **_: Any) -> None:
            pass

        def run_scenario(self, spec: ScenarioSpec, *, repeat: int, record_to: Path, **_: Any):
            calls.append((spec.scenario_id, repeat, record_to))
            meta = FixtureMeta(spec.scenario_id, spec.seed, repeat, T0, 1.0)
            return live.Recording(record_to, meta, 1, 0)

    monkeypatch.setattr(live, "LiveRunner", StubRunner)
    monkeypatch.setenv("INFRAMIND_EVAL_FIXTURE_DIR", str(tmp_path / "fx"))
    monkeypatch.setenv("INFRAMIND_EVAL_RESULTS_DIR", str(tmp_path / "results"))
    sid = "dev-podchaos-01-cartservice-pod-kill"
    blocked = "dev-stresschaos-04-cartservice-cpu-stress"

    assert main_mod.main(["--set", "dev", "--live", "--scenario", sid, "--scenario", blocked]) == 0
    assert [(c[0], c[1]) for c in calls] == [(sid, r) for r in range(5)]
    assert calls[3][2] == tmp_path / "fx" / sid / "r3"
    assert not (tmp_path / "results").exists()


def test_main_live_dry_run_prints_the_plan_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--live --dry-run` never builds a LiveRunner (no kubectl, no Redis, no fault)."""
    import inframind.evaluation.runner.live as live
    from inframind.evaluation.runner import main as main_mod

    class ExplodingRunner:
        def __init__(self, **_: Any) -> None:
            raise AssertionError("--dry-run must not construct a LiveRunner")

    monkeypatch.setattr(live, "LiveRunner", ExplodingRunner)
    monkeypatch.setenv("INFRAMIND_EVAL_FIXTURE_DIR", str(tmp_path / "fx"))
    monkeypatch.setenv("INFRAMIND_EVAL_RESULTS_DIR", str(tmp_path / "results"))
    sid = "dev-podchaos-01-cartservice-pod-kill"
    blocked = "dev-stresschaos-04-cartservice-cpu-stress"
    existing = tmp_path / "fx" / sid / "r0"
    existing.mkdir(parents=True)
    write_signals_gz(existing / "signals.ndjson.gz", [_signal(T0)])

    argv = ["--set", "dev", "--live", "--dry-run", "--scenario", sid, "--scenario", blocked]
    assert main_mod.main(argv) == 0

    out = capsys.readouterr().out
    assert f"{sid} r0  skip (fixture exists)" in out
    assert all(f"{sid} r{r}  record" in out for r in range(1, 5))
    assert f"{blocked}  skip (blocked" in out
    assert sorted(p.name for p in (tmp_path / "fx").iterdir()) == [sid]  # nothing new
    assert not (tmp_path / "results").exists()


def test_fresh_history_ignores_entries_from_before_a_restart() -> None:
    """Yesterday's entries sit behind a gap, so only the fresh run counts (the dev-03 bug)."""
    redis = fakeredis.FakeRedis()
    for offset in range(-90_000, -86_000, 30):  # an hour of history, a day ago
        _xadd(redis, stream_keys.SIGNALS, T0 + timedelta(seconds=offset), "{}")
    for offset in range(-120, 1, 30):  # two minutes since the restart
        _xadd(redis, stream_keys.SIGNALS, T0 + timedelta(seconds=offset), "{}")

    start = fresh_history_start(redis, T0, lookback_s=600, max_gap_s=120)
    assert start == T0 - timedelta(seconds=120)


def test_fresh_history_is_capped_at_the_lookback_and_none_when_stale() -> None:
    redis = fakeredis.FakeRedis()
    for offset in range(-3_600, 1, 30):
        _xadd(redis, stream_keys.SIGNALS, T0 + timedelta(seconds=offset), "{}")
    start = fresh_history_start(redis, T0, lookback_s=600, max_gap_s=120)
    assert start is not None
    assert T0 - start >= timedelta(seconds=600)
    # Ten minutes later with nothing published: the collectors are down.
    later = T0 + timedelta(seconds=600)
    assert fresh_history_start(redis, later, lookback_s=600, max_gap_s=120) is None
    assert fresh_history_start(fakeredis.FakeRedis(), T0, lookback_s=600, max_gap_s=120) is None


def test_run_scenario_waits_out_a_restart_gap(tmp_path: Path) -> None:
    """Stale history before a 1 h outage is ignored: it waits the full baseline again."""
    redis = fakeredis.FakeRedis()
    for offset in range(-7_200, -3_600, 30):  # before the outage
        _xadd(
            redis,
            stream_keys.SIGNALS,
            T0 + timedelta(seconds=offset),
            _signal(T0).model_dump_json(),
        )
    _seed_bus(redis, history_s=100, run_s=1500)  # after the restart
    clock = FakeClock(T0)
    recording = _runner(redis, clock, FakeKubectl(), tmp_path).run_scenario(
        _spec("dev-podchaos-01-cartservice-pod-kill"), repeat=0, record_to=tmp_path / "r0"
    )
    assert clock.slept[0] == pytest.approx(500.0)
    assert recording.meta.onset_ts == T0 + timedelta(seconds=500)


def test_run_scenario_refuses_when_the_collectors_are_down(tmp_path: Path) -> None:
    redis = fakeredis.FakeRedis()
    _xadd(redis, stream_keys.SIGNALS, T0 - timedelta(hours=2), "{}")
    runner = _runner(redis, FakeClock(T0), FakeKubectl(), tmp_path)
    with pytest.raises(LiveRunnerError, match="start the collectors"):
        runner.run_scenario(
            _spec("dev-podchaos-01-cartservice-pod-kill"), repeat=0, record_to=tmp_path / "r0"
        )


def test_back_to_back_repeats_keep_the_previous_fault_out_of_the_baseline(
    tmp_path: Path,
) -> None:
    """Repeat 1 waits until repeat 0's recording ended `baseline_s` ago (the live dev-01 bug).

    Without the wait the repeats ran 287 s apart, the previous kill sat inside the next 600 s
    baseline, and every later repeat opened its incident before its own fault.
    """
    redis = fakeredis.FakeRedis()
    _seed_bus(redis, history_s=900, run_s=3000)
    clock = FakeClock(T0)
    runner = _runner(redis, clock, FakeKubectl(), tmp_path)
    spec = _spec("dev-podchaos-01-cartservice-pod-kill")
    fixtures = tmp_path / "fixtures" / spec.scenario_id

    first = runner.run_scenario(spec, repeat=0, record_to=fixtures / "r0")
    assert first.meta.recorded_at is not None
    second = runner.run_scenario(spec, repeat=1, record_to=fixtures / "r1")

    gap = (second.meta.onset_ts - first.meta.recorded_at).total_seconds()
    assert gap == pytest.approx(600.0)
    baseline_start = second.meta.onset_ts - timedelta(seconds=600)
    assert baseline_start >= first.meta.recorded_at


def test_a_fault_from_an_aborted_run_still_delays_the_next_one(tmp_path: Path) -> None:
    """A recorder killed mid-fault leaves its marker; the next run waits it out."""
    redis = fakeredis.FakeRedis()
    _seed_bus(redis, history_s=900, run_s=3000)
    clock = FakeClock(T0)
    spec = _spec("dev-podchaos-01-cartservice-pod-kill")
    fixtures = tmp_path / "fixtures" / spec.scenario_id

    def killed(_seconds: float) -> None:
        raise KeyboardInterrupt

    aborted = _runner(redis, clock, FakeKubectl(), tmp_path)
    aborted._sleep = killed
    with pytest.raises(KeyboardInterrupt):
        aborted.run_scenario(spec, repeat=3, record_to=fixtures / "r3")
    assert (fixtures / "r3" / FAULT_MARKER).exists()

    resumed = _runner(redis, clock, FakeKubectl(), tmp_path)
    done = resumed.run_scenario(spec, repeat=3, record_to=fixtures / "r3")
    fault_over = T0 + timedelta(seconds=spec.duration_s + spec.cooldown_s)
    assert done.meta.onset_ts - timedelta(seconds=600) >= fault_over
    assert not (fixtures / "r3" / FAULT_MARKER).exists()


def test_read_signals_drops_late_published_lines_from_before_the_window() -> None:
    """Publish time is in range but `ts` is not: the line belongs to an earlier minute."""
    redis = fakeredis.FakeRedis()
    late = _signal(T0 - timedelta(seconds=240))  # observed 4 min before the window
    _xadd(redis, stream_keys.SIGNALS, T0 + timedelta(seconds=5), late.model_dump_json())
    inside = _signal(T0 + timedelta(seconds=10))
    _xadd(redis, stream_keys.SIGNALS, T0 + timedelta(seconds=11), inside.model_dump_json())
    got = read_signals(redis, T0, T0 + timedelta(seconds=60))
    assert [s.id for s in got] == [inside.id]


def _live_fixture(root: Path, *, burst_before_onset: int) -> Path:
    """A minimal live recording: quiet CPU samples and `burst_before_onset` error lines."""
    d = root / "r0"
    d.mkdir(parents=True)
    signals = [_signal(T0 + timedelta(seconds=s)) for s in range(-600, 120, 30)]
    if burst_before_onset:
        signals += [
            Signal(
                ts=T0 - timedelta(seconds=300, milliseconds=-i),
                source=Source.LOKI,
                type=SignalType.LOG,
                service="frontend",
                severity=Severity.CRITICAL,
                attrs={"message": "could not retrieve cart"},
            )
            for i in range(burst_before_onset)
        ]
    signals.sort(key=lambda s: (s.ts, s.id))
    (d / "signals.ndjson").write_text("".join(s.model_dump_json() + "\n" for s in signals))
    meta = FixtureMeta("dev-podchaos-01-cartservice-pod-kill", 1001, 0, T0, 240.0, T0)
    (d / "meta.json").write_text(json.dumps({**meta.as_dict(), "recorded_live": True}))
    (d / "graph.json").write_text('{"edges": [], "callers": {}}')
    return d


def test_only_an_incident_sized_burst_before_the_fault_blocks_scoring(tmp_path: Path) -> None:
    """An earlier fault (a burst far over the noise level) is refused; a blip is noise."""
    from inframind.evaluation.runner.main import _dirty_baseline

    spec = _spec("dev-podchaos-01-cartservice-pod-kill")
    blip = _live_fixture(tmp_path / "blip", burst_before_onset=6)
    earlier_fault = _live_fixture(tmp_path / "fault", burst_before_onset=80)
    clean = _live_fixture(tmp_path / "clean", burst_before_onset=0)
    assert _dirty_baseline(spec, blip, 0) is False
    assert _dirty_baseline(spec, earlier_fault, 0) is True
    assert _dirty_baseline(spec, clean, 0) is False


def test_compressed_signals_round_trip_byte_for_byte(tmp_path: Path) -> None:
    """Same signals, same bytes: the gzip header carries no time, so re-recording is diffable."""
    signals = [_signal(T0 + timedelta(seconds=i)) for i in range(20)]
    a, b = tmp_path / "a.ndjson.gz", tmp_path / "b.ndjson.gz"
    write_signals_gz(a, signals)
    write_signals_gz(b, signals)
    assert a.read_bytes() == b.read_bytes()
    assert [s.id for s in replay_signals(a)] == [s.id for s in signals]
    assert signals_file(tmp_path / "missing").name == "signals.ndjson"


def test_a_fault_the_runtime_refuses_is_reverted_and_never_recorded(tmp_path: Path) -> None:
    """The dev-02 case: apply is accepted, then Chaos Mesh reports the runtime refused it."""
    redis = fakeredis.FakeRedis()
    _seed_bus(redis, history_s=900, run_s=600)
    kubectl = FakeKubectl(chaos=RUNTIME_REFUSED)
    runner = _runner(redis, FakeClock(T0), kubectl, tmp_path)
    target = tmp_path / "r0"
    with pytest.raises(LiveRunnerError, match="expected docker:// but got containerd"):
        runner.run_scenario(
            _spec("dev-podchaos-01-cartservice-pod-kill"), repeat=0, record_to=target
        )
    assert kubectl.verbs() == ["apply", "get", "delete"]  # reverted at once
    assert not (target / "signals.ndjson.gz").exists()


def test_a_fault_never_confirmed_times_out_and_is_reverted(tmp_path: Path) -> None:
    redis = fakeredis.FakeRedis()
    _seed_bus(redis, history_s=900, run_s=600)
    pending = {"status": {"conditions": [{"type": "AllInjected", "status": "False"}]}}
    kubectl = FakeKubectl(chaos=pending)
    clock = FakeClock(T0)
    runner = _runner(redis, clock, kubectl, tmp_path, inject_timeout_s=10.0, inject_poll_s=2.0)
    with pytest.raises(LiveRunnerError, match="not confirmed in 10s"):
        runner.run_scenario(
            _spec("dev-podchaos-01-cartservice-pod-kill"), repeat=0, record_to=tmp_path / "r0"
        )
    assert kubectl.verbs()[-1] == "delete"
    assert clock.slept == [2.0] * 5


def test_a_config_change_that_creates_no_new_generation_is_reverted(tmp_path: Path) -> None:
    redis = fakeredis.FakeRedis()
    _seed_bus(redis, history_s=900, run_s=600)
    kubectl = FakeKubectl(deploy=[(3, 3)])  # the edit changed nothing
    runner = _runner(redis, FakeClock(T0), kubectl, tmp_path, inject_timeout_s=4.0)
    with pytest.raises(LiveRunnerError, match="did not take effect"):
        runner.run_scenario(
            _spec("test-config-06-cartservice-bad-redis-addr"), repeat=0, record_to=tmp_path / "r0"
        )
    assert kubectl.verbs()[-1] == "rollout"  # rollout undo


def test_the_status_readers_on_real_shapes() -> None:
    from inframind.evaluation.runner.live import chaos_injection_state, rollout_state

    assert chaos_injection_state(INJECTED) == (True, "")
    verdict, why = chaos_injection_state(RUNTIME_REFUSED)
    assert verdict is False and "containerd" in why
    assert chaos_injection_state({})[0] is None

    def deploy(gen: int, observed: int, updated: int, total: int) -> dict:
        return {
            "metadata": {"generation": gen},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": observed,
                "updatedReplicas": updated,
                "replicas": total,
            },
        }

    assert rollout_state(deploy(4, 4, 1, 1), 3)[0] is True
    assert rollout_state(deploy(4, 3, 0, 1), 3)[0] is None  # not observed yet
    # New pod not Ready: the old pod still serves, so the fault has not reached traffic.
    assert rollout_state(deploy(4, 4, 1, 2), 3)[0] is None
    assert rollout_state(deploy(3, 3, 1, 1), 3)[0] is None  # the edit changed nothing


def test_a_recording_whose_fault_never_took_effect_is_not_scored(tmp_path: Path) -> None:
    """dev-02: `fault_injected: false` yields no row, whatever the scenario's status."""
    from inframind.evaluation.runner.main import _no_fault, _run_one

    spec = _spec("dev-podchaos-01-cartservice-pod-kill")
    d = _live_fixture(tmp_path / spec.scenario_id, burst_before_onset=0)
    assert _no_fault(spec, d, 0) is False
    meta = json.loads((d / "meta.json").read_text())
    (d / "meta.json").write_text(json.dumps({**meta, "fault_injected": False}))
    assert _no_fault(spec, d, 0) is True
    params = EvalParams(fixture_dir=str(tmp_path))
    assert (
        _run_one(spec=spec, params=params, repeat=0, run_baselines=True, run_ablations=False) == []
    )
