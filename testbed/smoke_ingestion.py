"""P3 gate: do all four ingestion paths deliver real data end to end? (`make smoke-ingestion`)

Starts the API (Alertmanager webhook) and the Prometheus, Loki and Jaeger collectors against the
live testbed, drives traffic through the Online Boutique frontend including one uniquely tagged
failing request, sends one synthetic alert through the *real* Alertmanager, then waits for the
evidence on the real pinned streams. Exit code 0 only if every check passes.

Needs the testbed up (`make up`) and port 8080 free: the in-cluster Alertmanager receiver
targets the host at port 8080 (testbed/helm-values/kube-prometheus-stack.yaml).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from redis.asyncio import Redis

from inframind.common import stream_keys
from inframind.common.graph_update import GraphUpdate
from inframind.common.signal import Signal, Source

REPO = Path(__file__).resolve().parents[1]
EXPECTED_EDGES = {
    ("frontend", "productcatalogservice"),
    ("frontend", "recommendationservice"),
    ("recommendationservice", "productcatalogservice"),
}
MIN_PROMETHEUS_SERVICES = 6
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check: a short stable name, pass/fail, and a human-readable detail."""

    name: str
    ok: bool
    detail: str


def edges_present(updates: Iterable[GraphUpdate]) -> set[tuple[str, str]]:
    """Return the distinct (caller, callee) pairs in `updates`."""
    return {(u.caller, u.callee) for u in updates}


def distinct_services(signals: Iterable[Signal], source: Source) -> set[str]:
    """Return the routable service names that `source` produced signals for."""
    return {s.service for s in signals if s.source is source and s.is_routable}


def evaluate(signals: list[Signal], updates: list[GraphUpdate], run_id: str) -> list[CheckResult]:
    """Judge the evidence collected during one smoke run. `run_id` ties it to this run's inputs."""
    services = distinct_services(signals, Source.PROMETHEUS)
    prometheus = CheckResult(
        "prometheus",
        len(services) >= MIN_PROMETHEUS_SERVICES,
        f"{len(services)} services (need {MIN_PROMETHEUS_SERVICES})",
    )
    loki_hit = any(
        s.source is Source.LOKI
        and s.service == "frontend"
        and s.attrs.get("level") == "error"
        and s.attrs.get("grpc_code") == "NotFound"
        and run_id in str(s.attrs.get("message", ""))
        for s in signals
    )
    loki = CheckResult(
        "loki",
        loki_hit,
        "frontend error with grpc_code=NotFound" if loki_hit else "no matching error",
    )
    missing = EXPECTED_EDGES - edges_present(updates)
    edges = CheckResult(
        "jaeger-edges",
        not missing,
        "all 3 expected edges"
        if not missing
        else "missing: " + ", ".join(f"{a}->{b}" for a, b in sorted(missing)),
    )
    jaeger_hit = any(
        s.source is Source.JAEGER
        and s.trace_id is not None
        and _TRACE_ID.match(s.trace_id)
        and run_id in str(s.attrs.get("status_description", ""))
        for s in signals
    )
    jaeger = CheckResult(
        "jaeger-error",
        bool(jaeger_hit),
        "error span with a W3C trace id" if jaeger_hit else "no matching error span",
    )
    alert_hit = any(
        s.source is Source.ALERTMANAGER
        and s.service == "frontend"
        and s.attrs.get("labels", {}).get("smoke_run") == run_id
        for s in signals
    )
    alert = CheckResult(
        "alertmanager",
        alert_hit,
        "synthetic alert delivered by Alertmanager" if alert_hit else "no matching alert",
    )
    return [prometheus, loki, edges, jaeger, alert]


# ----------------------------------------------------------------------------- live driver


def say(status: str, text: str) -> None:
    print(f"[smoke-ingestion] {status}: {text}", flush=True)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


async def reachable(redis: Redis) -> list[CheckResult]:
    probes = {
        "redis": None,
        "prometheus": _env("PROMETHEUS_URL", "http://localhost:30090")
        + "/api/v1/status/runtimeinfo",
        "loki": _env("LOKI_URL", "http://localhost:30100") + "/loki/api/v1/labels",
        "jaeger": _env("JAEGER_URL", "http://localhost:30686") + "/api/services",
        "alertmanager": _env("ALERTMANAGER_URL", "http://localhost:30093") + "/api/v2/status",
    }
    results: list[CheckResult] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, url in probes.items():
            try:
                if url is None:
                    await redis.ping()
                else:
                    (await client.get(url)).raise_for_status()
                results.append(CheckResult(name, True, "reachable"))
            except Exception as e:  # any failure means the testbed is not ready
                results.append(CheckResult(name, False, f"{type(e).__name__}: {e}"))
    return results


async def last_id(redis: Redis, stream: str) -> str:
    entries = await redis.xrevrange(stream, count=1)
    return entries[0][0].decode() if entries else "0-0"


async def read_new(redis: Redis, stream: str, after: str) -> list[bytes]:
    lo = f"({after}" if after != "0-0" else "-"
    return [fields[b"data"] for _, fields in await redis.xrange(stream, min=lo)]


def spawn(module: str, env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", module],
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_all(procs: list[subprocess.Popen[bytes]]) -> None:
    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
    for p in procs:
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()


def kubectl(*args: str) -> subprocess.CompletedProcess[str]:
    kubeconfig = os.environ.get("KUBECONFIG", str(REPO / "testbed" / ".kube" / "config"))
    return subprocess.run(
        ["kubectl", "--context", "kind-inframind", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "KUBECONFIG": kubeconfig},
        check=False,
    )


async def drive_traffic(base: str, run_id: str, stop: asyncio.Event) -> None:
    paths = ["/", "/product/OLJCESPC7Z", f"/product/NOPE-{run_id}", "/product/66VCHSJNUP"]
    async with httpx.AsyncClient(base_url=base, timeout=8.0) as client:
        while not stop.is_set():
            for path in paths:
                with contextlib.suppress(httpx.HTTPError):
                    await client.get(path)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=2.0)


async def send_synthetic_alert(run_id: str, pod: str) -> None:
    now = datetime.now(UTC)
    alert = {
        "labels": {
            "alertname": "InframindSmoke",
            "smoke_run": run_id,
            "severity": "warning",
            "namespace": "default",
            "pod": pod,
        },
        "annotations": {"summary": "P3 ingestion smoke test alert"},
        "startsAt": now.isoformat(),
        "endsAt": (now + timedelta(minutes=3)).isoformat(),
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _env("ALERTMANAGER_URL", "http://localhost:30093") + "/api/v2/alerts", json=[alert]
        )
        resp.raise_for_status()


async def amain() -> int:
    run_id = uuid.uuid4().hex[:6]
    redis = Redis.from_url(_env("REDIS_URL", "redis://localhost:6379/0"))
    say("INFO", f"P3 ingestion gate, run {run_id}")
    reach = await reachable(redis)
    for r in reach:
        say("OK" if r.ok else "FAIL", f"C1 {r.name}: {r.detail}")
    if not all(r.ok for r in reach):
        say("FAIL", "testbed is not ready; run `make up` first")
        return 1
    api_port = int(_env("INFRAMIND_API_PORT", "8080"))
    if not port_is_free(api_port) or not port_is_free(18081):
        say(
            "FAIL",
            f"ports {api_port} and 18081 must be free (the Alertmanager receiver targets {api_port})",
        )
        return 1
    pod = kubectl(
        "get", "pods", "-l", "app=frontend", "-o", "jsonpath={.items[0].metadata.name}"
    ).stdout.strip()
    if not pod:
        say("FAIL", "no frontend pod found; is Online Boutique running?")
        return 1

    sig_after = await last_id(redis, stream_keys.SIGNALS)
    gra_after = await last_id(redis, stream_keys.GRAPH_UPDATES)
    base_env = {"REDIS_URL": _env("REDIS_URL", "redis://localhost:6379/0")}
    secret = _env("SMOKE_WEBHOOK_SECRET", "dev-secret")
    procs = [
        spawn(
            "inframind.api.app",
            {
                **base_env,
                "INFRAMIND_ALERT_WEBHOOK_SECRET": secret,
                "INFRAMIND_API_PORT": str(api_port),
            },
        ),
        spawn("inframind.ingestion.prometheus", {**base_env, "PROM_INTERVAL_S": "5"}),
        spawn("inframind.ingestion.loki", {**base_env, "LOKI_INTERVAL_S": "5"}),
        spawn("inframind.ingestion.jaeger", {**base_env, "JAEGER_INTERVAL_S": "5"}),
    ]
    forward = subprocess.Popen(
        ["kubectl", "--context", "kind-inframind", "port-forward", "svc/frontend", "18081:80"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "KUBECONFIG": os.environ.get("KUBECONFIG", str(REPO / "testbed" / ".kube" / "config")),
        },
    )
    stop = asyncio.Event()
    traffic: asyncio.Task[None] | None = None
    results: list[CheckResult] = []
    try:
        await asyncio.sleep(3)
        traffic = asyncio.create_task(drive_traffic("http://localhost:18081", run_id, stop))
        await send_synthetic_alert(run_id, pod)
        deadline = asyncio.get_running_loop().time() + float(_env("SMOKE_TIMEOUT_S", "90"))
        while True:
            await asyncio.sleep(3)
            signals = [
                Signal.model_validate_json(raw)
                for raw in await read_new(redis, stream_keys.SIGNALS, sig_after)
            ]
            updates = [
                GraphUpdate.model_validate_json(raw)
                for raw in await read_new(redis, stream_keys.GRAPH_UPDATES, gra_after)
            ]
            results = evaluate(signals, updates, run_id)
            if all(r.ok for r in results) or asyncio.get_running_loop().time() > deadline:
                break
    finally:
        stop.set()
        if traffic is not None:
            await traffic
        forward.terminate()
        stop_all(procs)
        await redis.aclose()
    for r in results:
        say("OK" if r.ok else "FAIL", f"C4 {r.name}: {r.detail}")
    passed = sum(r.ok for r in results)
    say(
        "OK" if passed == len(results) else "FAIL",
        f"P3 gate: {passed}/{len(results)} checks passed",
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
