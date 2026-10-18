"""P4 gate: does a real fault turn into one incident, with frozen baselines, then resolve?

(`make smoke-detection`.) Runs the Prometheus, Loki and Jaeger collectors and the correlation
worker against the live testbed, lets the detectors warm up on a trickle of traffic, checks that
a quiet minute raises nothing, then hits the frontend with a burst of parallel requests (the
fault). It checks the incident the worker publishes: opened within the proposal's 60 s MTTD,
baselines frozen while the fault kept firing, resolved after the burst ends.

The worker runs in-process on a throwaway consumer group at the tail of `stream:signals` and
writes to its own incidents stream, so the real streams and groups are left alone.

Needs the testbed up (`make up`). Takes about 7 minutes; the detectors need real warm-up time
(the cAdvisor scrape interval is 30 s). Exit code 0 only if every check passes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from redis.asyncio import Redis

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ingestion import CheckResult, port_is_free, reachable, say, spawn, stop_all

from inframind.common import stream_keys
from inframind.common.anomaly import Anomaly
from inframind.common.incident import Incident
from inframind.common.logging import configure_logging
from inframind.correlation.tracker import IncidentTracker
from inframind.correlation.worker import CorrelationWorker
from inframind.detection.engine import DetectionEngine

TARGET = "frontend"
MTTD_S = 60.0  # proposal success criterion: MTTD under 60 s
RESOLVE_SLACK_S = 150.0  # CPU stays high ~70 s after a burst, plus polling and scrape delay
METRIC_DETECTORS = ("zscore", "ewma")
FWD_PORT = 18084


def evaluate(
    incidents: list[Incident], burst_started: datetime, burst_ended: datetime, cooldown_s: float
) -> list[CheckResult]:
    """Judge the incidents published during one run; `incidents` are in publish order."""
    first: dict[str, Incident] = {}
    for incident in incidents:
        first.setdefault(incident.id, incident)
    early = [i for i in first.values() if i.opened_at < burst_started]
    quiet = CheckResult(
        "quiet",
        not early,
        "no incident before the burst"
        if not early
        else "incident before the burst for "
        + ", ".join(sorted({s for i in early for s in i.services})),
    )

    target = next(
        (i for i in first.values() if i.services == [TARGET] and i.opened_at >= burst_started), None
    )
    if target is None:
        gone = "no incident opened for the burst target"
        return [
            quiet,
            CheckResult("detected", False, gone),
            CheckResult("frozen", False, gone),
            CheckResult("resolved", False, gone),
        ]
    delay = (target.opened_at - burst_started).total_seconds()
    detected = CheckResult(
        "detected",
        delay <= MTTD_S,
        f"opened {delay:.0f}s after the burst began (limit {MTTD_S:.0f}s)",
    )

    snapshots = [i for i in incidents if i.id == target.id]
    last = max(snapshots, key=lambda i: i.revision)
    frozen = _frozen_check(last.anomalies)

    closed = next((i for i in snapshots if i.state == "resolved"), None)
    if closed is None or closed.closed_at is None:
        resolved = CheckResult("resolved", False, "the incident never resolved")
    else:
        took = (closed.closed_at - burst_ended).total_seconds()
        limit = cooldown_s + RESOLVE_SLACK_S
        resolved = CheckResult(
            "resolved",
            took <= limit,
            f"resolved {took:.0f}s after the burst ended (limit {limit:.0f}s)",
        )
    return [quiet, detected, frozen, resolved]


def target_resolved(incidents: list[Incident], burst_started: datetime) -> bool:
    """Return whether the burst target's incident has a resolved snapshot (others do not count)."""
    ids = {i.id for i in incidents if i.services == [TARGET] and i.opened_at >= burst_started}
    return any(i.id in ids and i.state == "resolved" for i in incidents)


def describe(incidents: list[Incident]) -> list[str]:
    """Render every published snapshot and its anomalies, for diagnosing a failed run."""
    if not incidents:
        return ["no incidents were published"]
    lines: list[str] = []
    for i in incidents:
        closed = f" closed {i.closed_at:%H:%M:%S}" if i.closed_at else ""
        lines.append(
            f"  incident {i.id[-6:]} rev {i.revision} {i.state:8} {','.join(i.services)} "
            f"opened {i.opened_at:%H:%M:%S} updated {i.updated_at:%H:%M:%S}{closed} "
            f"{i.severity.value} ({len(i.anomalies)} anomalies)"
        )
        for a in i.anomalies[:8]:
            what = a.attrs.get("metric") or f"{a.attrs.get('count', '?')} errors"
            lines.append(
                f"      {a.ts:%H:%M:%S} {a.detector:18} {what!s:26} "
                f"score {a.score:+8.1f} frozen={a.attrs.get('frozen')}"
            )
    return lines


def _frozen_check(anomalies: list[Anomaly]) -> CheckResult:
    """The baseline must not move while the fault keeps firing (D4)."""
    for detector in METRIC_DETECTORS:
        mine = [a for a in anomalies if a.detector == detector]
        later = mine[1:]  # the first anomaly opens the incident; the freeze applies from then on
        frozen = [a for a in later if a.attrs.get("frozen") is True]
        if len(frozen) < 2 or len(frozen) != len(later):
            continue
        means = [float(a.attrs["mean"]) for a in frozen]
        if max(means) - min(means) < 1e-12:
            return CheckResult(
                "frozen",
                True,
                f"{detector}: {len(frozen)} anomalies scored against one frozen baseline",
            )
    return CheckResult(
        "frozen", False, "no detector fired repeatedly against a constant frozen baseline"
    )


# ----------------------------------------------------------------------------- live driver


async def trickle(base: str, stop: asyncio.Event) -> None:
    """Light, steady traffic so traces and logs flow while the baselines warm up."""
    async with httpx.AsyncClient(base_url=base, timeout=8.0) as client:
        while not stop.is_set():
            with contextlib.suppress(httpx.HTTPError):
                await client.get("/")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=2.0)


async def burst(base: str, clients: int, seconds: float) -> int:
    """Hammer the frontend from `clients` parallel loops; return the request count."""
    deadline = time.monotonic() + seconds
    limits = httpx.Limits(max_connections=clients)

    async def loop(client: httpx.AsyncClient) -> int:
        sent = 0
        while time.monotonic() < deadline:
            with contextlib.suppress(httpx.HTTPError):
                await client.get("/product/OLJCESPC7Z")
                sent += 1
        return sent

    async with httpx.AsyncClient(base_url=base, timeout=10.0, limits=limits) as client:
        return sum(await asyncio.gather(*[loop(client) for _ in range(clients)]))


async def read_incidents(redis: Redis, stream: str) -> list[Incident]:
    return [Incident.model_validate_json(f[b"data"]) for _, f in await redis.xrange(stream)]


async def amain() -> int:
    configure_logging()
    run_id = uuid.uuid4().hex[:6]
    warmup_s = float(os.environ.get("SMOKE_WARMUP_S", "150"))
    quiet_s = float(os.environ.get("SMOKE_QUIET_S", "60"))
    burst_s = float(os.environ.get("SMOKE_BURST_S", "45"))
    cooldown_s = float(os.environ.get("SMOKE_COOLDOWN_S", "45"))
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    say("INFO", f"P4 detection gate, run {run_id}")
    probes = await reachable(redis)
    for r in probes:
        say("OK" if r.ok else "FAIL", f"C1 {r.name}: {r.detail}")
    if not all(r.ok for r in probes):
        say("FAIL", "testbed is not ready; run `make up` first")
        return 1
    if not port_is_free(FWD_PORT):
        say("FAIL", f"port {FWD_PORT} must be free")
        return 1

    group, out_stream = f"smoke-detection-{run_id}", f"stream:test-incidents-{run_id}"
    engine = DetectionEngine.default(
        {
            "INFRAMIND_DETECTION_ZSCORE_MIN_SAMPLES": "8",
            "INFRAMIND_DETECTION_EWMA_MIN_SAMPLES": "8",
            "BASELINE_FREEZE_WINDOW": "300",
        }
    )
    worker = CorrelationWorker(
        redis,
        engine,
        IncidentTracker(engine.coordinator, cooldown_s=cooldown_s),
        group=group,
        consumer="smoke",
        block_ms=500,
        start_id="$",
        out_stream=out_stream,
    )
    stop_worker, stop_traffic = asyncio.Event(), asyncio.Event()
    worker_task = asyncio.create_task(worker.run(stop_worker))
    await asyncio.sleep(1)  # the group must exist at the tail before the collectors publish

    env = {"REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:6379/0")}
    procs = [
        spawn("inframind.ingestion.prometheus", {**env, "PROM_INTERVAL_S": "15"}),
        spawn("inframind.ingestion.loki", {**env, "LOKI_INTERVAL_S": "15"}),
        spawn("inframind.ingestion.jaeger", {**env, "JAEGER_INTERVAL_S": "15"}),
    ]
    forward = subprocess.Popen(
        [
            "kubectl",
            "--context",
            "kind-inframind",
            "port-forward",
            "svc/frontend",
            f"{FWD_PORT}:80",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "KUBECONFIG": os.environ.get("KUBECONFIG", "testbed/.kube/config")},
    )
    base = f"http://localhost:{FWD_PORT}"
    traffic: asyncio.Task[None] | None = None
    results: list[CheckResult] = []
    published: list[Incident] = []
    try:
        await asyncio.sleep(3)
        traffic = asyncio.create_task(trickle(base, stop_traffic))
        say("INFO", f"warming the baselines for {warmup_s:.0f}s")
        await asyncio.sleep(warmup_s)
        say("INFO", f"quiet period {quiet_s:.0f}s: nothing may open")
        await asyncio.sleep(quiet_s)
        started = datetime.now().astimezone()
        say("INFO", f"BURST for {burst_s:.0f}s (40 parallel clients)")
        sent = await burst(base, 40, burst_s)
        ended = datetime.now().astimezone()
        say("INFO", f"burst sent {sent} requests; waiting for the incident to resolve")
        deadline = time.monotonic() + cooldown_s + 240
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            if target_resolved(await read_incidents(redis, out_stream), started):
                break
        published = await read_incidents(redis, out_stream)
        results = evaluate(published, started, ended, cooldown_s)
    finally:
        stop_traffic.set()
        stop_worker.set()
        if traffic is not None:
            await traffic
        with contextlib.suppress(Exception):
            await asyncio.wait_for(worker_task, timeout=10)
        forward.terminate()
        stop_all(procs)
        with contextlib.suppress(Exception):
            await redis.xgroup_destroy(stream_keys.SIGNALS, group)
        await redis.delete(out_stream)
        await redis.aclose()
    for r in results:
        say("OK" if r.ok else "FAIL", f"C5 {r.name}: {r.detail}")
    if not all(r.ok for r in results):
        say("INFO", "published snapshots (for diagnosis):")
        print("\n".join(describe(published)), flush=True)
    passed = sum(r.ok for r in results)
    say(
        "OK" if results and passed == len(results) else "FAIL",
        f"P4 gate: {passed}/{len(results)} checks passed",
    )
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
