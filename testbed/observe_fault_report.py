"""Timeline report for `make observe-fault`: what the streams showed before, during, after a fault.

Pure builders (`edge_series`, `p95_series`, `cpu_series`, `error_series`, `format_timeline`) plus a
small CLI that reads a run directory written by `observe_fault.sh`:

    python testbed/observe_fault_report.py testbed/.observe/<run-id>
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from redis.asyncio import Redis

from inframind.common import stream_keys
from inframind.common.graph_update import GraphUpdate
from inframind.common.signal import Signal, SignalType, Source

BUCKET_S = 30
Series = dict[str, dict[int, float]]


@dataclass(frozen=True)
class Markers:
    """Epoch seconds that delimit a run: start, fault injected, fault expected over, end."""

    start: int
    inject: int
    fault_end: int
    end: int


def _bucket(markers: Markers, epoch: float) -> int | None:
    if not markers.start <= epoch <= markers.end:
        return None
    return int((epoch - markers.start) // BUCKET_S)


def phase(markers: Markers, bucket: int) -> str:
    """Label a bucket `base`, `FAULT` or `after` by where its first second falls."""
    lo = markers.start + bucket * BUCKET_S
    if lo < markers.inject:
        return "base"
    return "FAULT" if lo < markers.fault_end else "after"


def _collect(
    items: Iterable[tuple[str, float, float]], markers: Markers, agg: Callable[[list[float]], float]
) -> Series:
    grouped: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for name, epoch, value in items:
        bucket = _bucket(markers, epoch)
        if bucket is not None:
            grouped[name][bucket].append(value)
    return {n: {b: agg(v) for b, v in sorted(buckets.items())} for n, buckets in grouped.items()}


def edge_series(updates: Iterable[GraphUpdate], markers: Markers) -> Series:
    """Mean call latency in ms of each `caller->callee` edge per bucket."""
    return _collect(
        ((f"{u.caller}->{u.callee}", u.observed_at.timestamp(), u.latency_ms) for u in updates),
        markers,
        statistics.mean,
    )


def p95_series(signals: Iterable[Signal], markers: Markers) -> Series:
    """Worst per-poll server-span p95 in ms of each service per bucket (Jaeger latency signals)."""
    return _collect(
        (
            (s.service, s.ts.timestamp(), float(s.attrs["p95_ms"]))
            for s in signals
            if s.source is Source.JAEGER and s.attrs.get("metric") == "span_latency"
        ),
        markers,
        max,
    )


def cpu_series(signals: Iterable[Signal], markers: Markers) -> Series:
    """Mean CPU in millicores of each service per bucket (Prometheus signals)."""
    return _collect(
        (
            (s.service, s.ts.timestamp(), float(s.attrs["value"]) * 1000)
            for s in signals
            if s.source is Source.PROMETHEUS and s.attrs.get("metric") == "cpu_cores"
        ),
        markers,
        statistics.mean,
    )


def error_series(signals: Iterable[Signal], markers: Markers) -> Series:
    """Count of Loki error lines and Jaeger error spans per `source:service` and bucket."""

    def keyed() -> Iterable[tuple[str, float, float]]:
        for s in signals:
            if s.source is Source.LOKI and s.type is SignalType.LOG:
                yield f"loki:{s.service}", s.ts.timestamp(), 1.0
            elif s.source is Source.JAEGER and "span_id" in s.attrs:
                yield f"jaeger:{s.service}", s.ts.timestamp(), 1.0

    return _collect(keyed(), markers, lambda values: float(len(values)))


def _table(title: str, series: Series, fmt: str, markers: Markers, buckets: int) -> list[str]:
    width = max([34, *(len(name) + 2 for name in series)])
    lines = [
        f"## {title}",
        "bucket(+s):".ljust(width) + "".join(f"{b * BUCKET_S:>7}" for b in range(buckets)),
    ]
    lines.append("phase:".ljust(width) + "".join(f"{phase(markers, b):>7}" for b in range(buckets)))
    for name in sorted(series):
        cells = "".join(
            f"{fmt.format(series[name][b]):>7}" if b in series[name] else f"{'.':>7}"
            for b in range(buckets)
        )
        lines.append(name.ljust(width) + cells)
    return [*lines, ""]


def format_timeline(signals: list[Signal], updates: list[GraphUpdate], markers: Markers) -> str:
    """Render the per-bucket tables for edge latency, p95, CPU, errors, and the alerts seen."""
    buckets = (markers.end - markers.start) // BUCKET_S + 1
    lines = [
        f"run: {markers.end - markers.start}s, fault from +{markers.inject - markers.start}s "
        f"to +{markers.fault_end - markers.start}s ({len(signals)} signals, {len(updates)} graph updates)",
        "",
    ]
    lines += _table(
        "graph edge latency ms (mean)", edge_series(updates, markers), "{:.1f}", markers, buckets
    )
    lines += _table(
        "jaeger server-span p95 ms (worst)",
        p95_series(signals, markers),
        "{:.1f}",
        markers,
        buckets,
    )
    lines += _table(
        "prometheus CPU millicores (mean)", cpu_series(signals, markers), "{:.0f}", markers, buckets
    )
    lines += _table(
        "error signals (count)", error_series(signals, markers), "{:.0f}", markers, buckets
    )
    alerts = sorted(
        {
            (s.service, str(s.attrs.get("alertname", "")))
            for s in signals
            if s.source is Source.ALERTMANAGER
        }
    )
    lines.append(f"alertmanager signals: {alerts if alerts else 'none'}")
    return "\n".join(lines)


async def _load(run_dir: Path) -> tuple[Markers, list[Signal], list[GraphUpdate]]:
    env = dict(
        line.split("=", 1) for line in (run_dir / "markers.env").read_text().split() if "=" in line
    )
    markers = Markers(int(env["start"]), int(env["inject"]), int(env["fault_end"]), int(env["end"]))
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:

        async def read(stream: str, after: str) -> list[bytes]:
            lo = f"({after}" if after != "0-0" else "-"
            return [fields[b"data"] for _, fields in await redis.xrange(stream, min=lo)]

        raw_signals = await read(stream_keys.SIGNALS, env["sig_after"])
        raw_updates = await read(stream_keys.GRAPH_UPDATES, env["gra_after"])
    finally:
        await redis.aclose()
    (run_dir / "signals.ndjson").write_bytes(b"\n".join(raw_signals))
    (run_dir / "updates.ndjson").write_bytes(b"\n".join(raw_updates))
    return (
        markers,
        [Signal.model_validate_json(r) for r in raw_signals],
        [GraphUpdate.model_validate_json(r) for r in raw_updates],
    )


def main(argv: list[str]) -> int:
    """Print the timeline for the run directory named in `argv[1]`."""
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    markers, signals, updates = asyncio.run(_load(Path(argv[1])))
    print(format_timeline(signals, updates, markers))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
