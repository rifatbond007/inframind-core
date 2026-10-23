"""The pure timeline builder behind `make observe-fault`: bucketing, phases, and the table."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inframind.common.graph_update import GraphUpdate
from inframind.common.signal import Signal, SignalType, Source

SCRIPT = Path(__file__).resolve().parents[2] / "testbed" / "observe_fault_report.py"
spec = importlib.util.spec_from_file_location("observe_fault_report", SCRIPT)
assert spec and spec.loader
report = importlib.util.module_from_spec(spec)
sys.modules["observe_fault_report"] = report
spec.loader.exec_module(report)

T = 1_792_000_000
MARKERS = report.Markers(start=T, inject=T + 90, fault_end=T + 240, end=T + 330)
EDGE = "recommendationservice->productcatalogservice"


def at(offset: int) -> datetime:
    return datetime.fromtimestamp(T + offset, tz=UTC)


def update(offset: int, latency: float) -> GraphUpdate:
    return GraphUpdate(
        caller="recommendationservice",
        callee="productcatalogservice",
        latency_ms=latency,
        observed_at=at(offset),
    )


def signal(offset: int, source: Source, kind: SignalType, service: str, **attrs: Any) -> Signal:
    return Signal(ts=at(offset), source=source, type=kind, service=service, attrs=attrs)


def test_phases_split_baseline_fault_and_recovery() -> None:
    labels = [report.phase(MARKERS, b) for b in range(11)]
    assert labels == ["base"] * 3 + ["FAULT"] * 5 + ["after"] * 3


def test_edge_latency_is_averaged_per_30_second_bucket() -> None:
    updates = [update(10, 1.0), update(20, 3.0), update(100, 200.0), update(130, 210.0)]
    series = report.edge_series(updates, MARKERS)
    assert series[EDGE] == {0: 2.0, 3: 200.0, 4: 210.0}


def test_events_outside_the_run_are_ignored() -> None:
    series = report.edge_series([update(-5, 9.0), update(400, 9.0), update(10, 1.0)], MARKERS)
    assert series[EDGE] == {0: 1.0}


def test_p95_takes_the_worst_value_in_a_bucket() -> None:
    sigs = [
        signal(10, Source.JAEGER, SignalType.TRACE, "frontend", metric="span_latency", p95_ms=4.0),
        signal(20, Source.JAEGER, SignalType.TRACE, "frontend", metric="span_latency", p95_ms=9.0),
        signal(10, Source.JAEGER, SignalType.TRACE, "frontend", span_id="x"),  # error span, ignored
    ]
    assert report.p95_series(sigs, MARKERS) == {"frontend": {0: 9.0}}


def test_cpu_is_reported_in_millicores() -> None:
    sigs = [
        signal(
            10, Source.PROMETHEUS, SignalType.METRIC, "adservice", metric="cpu_cores", value=0.25
        )
    ]
    assert report.cpu_series(sigs, MARKERS) == {"adservice": {0: 250.0}}


def test_errors_are_counted_by_source_and_service() -> None:
    sigs = [
        signal(100, Source.LOKI, SignalType.LOG, "frontend", level="error"),
        signal(105, Source.LOKI, SignalType.LOG, "frontend", level="error"),
        signal(110, Source.JAEGER, SignalType.TRACE, "frontend", span_id="s"),
    ]
    series = report.error_series(sigs, MARKERS)
    assert series["loki:frontend"] == {3: 2}
    assert series["jaeger:frontend"] == {3: 1}


def test_timeline_shows_the_latency_jump_inside_the_fault_columns() -> None:
    updates = [update(10, 1.0), update(100, 200.0), update(130, 205.0), update(300, 1.5)]
    text = report.format_timeline([], updates, MARKERS)
    assert "FAULT" in text and "base" in text and "after" in text
    row = next(line for line in text.splitlines() if line.startswith(EDGE))
    cells = row[len(EDGE) :].split()
    assert cells[0] == "1.0" and cells[3] == "200.0" and cells[4] == "205.0" and cells[10] == "1.5"
    assert cells[1] == "." and cells[7] == "."  # no samples in those buckets


def test_timeline_of_an_empty_run_does_not_crash() -> None:
    assert "FAULT" in report.format_timeline([], [], MARKERS)
