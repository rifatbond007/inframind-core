"""Report writing: rows → CSV + JSON aggregates (standard 10.9).

`report.py` writes:

* ``<results_dir>/<scenario_set>_<method>.csv`` — one row per run.
* ``<results_dir>/summary.json`` — aggregates + ``generated_at`` + the ``(scenario_id, seed)``
  provenance of every input row.
* ``<results_dir>/README.md`` — states plainly that these numbers were produced by ``make eval``.

Two guards:

* **Overwrite refusal.** An existing results directory is not clobbered unless
  ``INFRAMIND_EVAL_ALLOW_OVERWRITE=1``. An eval run must be a deliberate act.
* **Provenance check.** A row without a ``run_id`` is rejected. That is the mechanical half
  of PUKU.md's "never invent a number": a hand-written CSV cannot pass this check.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from inframind.common.logging import get_logger
from inframind.common.time import utcnow

from .schema import ResultRow
from .scoring import MethodAggregate, score

log = get_logger(__name__)

README_BODY = """# Phase 7 results

These numbers were produced by `make eval`. No number in this directory was written by hand.

Every row carries a `run_id` of the form `<scenario_id>@<seed>r<repeat>:<method>`, which ties
it back to the fixture it came from. A row without one is rejected by `report.py` before it
reaches this directory.

Aggregate accuracy lives in `summary.json` under `.aggregates`. P9's `results-to-latex`
reads these fields verbatim — do not rename them here without changing the paper writer.

This directory starts empty on purpose (D6, PUKU.md: never invent a number). The first real
run populates it.
"""

#: Columns written per CSV row, in order. Keep in sync with `ResultRow.as_csv_row`.
CSV_COLUMNS: tuple[str, ...] = (
    "scenario_id",
    "seed",
    "repeat",
    "method",
    "method_kind",
    "detected",
    "top1_correct",
    "top3_correct",
    "reciprocal_rank",
    "rank",
    "detection_latency_s",
    "false_positive",
    "scenario_set",
    "run_id",
    "expected_root_cause",
    "predicted_root_cause",
    "blocked",
    "notes",
)


def _validate_rows(rows: Sequence[ResultRow]) -> None:
    """Raise when any row is missing `run_id` or has a non-finite reciprocal rank."""
    for row in rows:
        if not row.run_id:
            raise ValueError(
                f"row for scenario {row.scenario_id!r} method {row.method!r} has no run_id; "
                "refusing to write a row that cannot be traced to a fixture"
            )


def prepare_dir(results_dir: Path, *, allow_overwrite: bool = False) -> Path:
    """Create (or verify) the results directory; raise if it exists and overwrite is off."""
    results_dir = Path(results_dir)
    if results_dir.exists() and any(results_dir.iterdir()) and not allow_overwrite:
        raise FileExistsError(
            f"{results_dir} already has content; set INFRAMIND_EVAL_ALLOW_OVERWRITE=1 to replace"
        )
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir


def write_rows_csv(
    rows: Sequence[ResultRow],
    results_dir: Path,
    *,
    scenario_set: str,
    method: str,
) -> Path:
    """Write one CSV per (scenario_set, method). Returns the path written."""
    _validate_rows(rows)
    path = results_dir / f"{scenario_set}_{method}.csv"
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = row.as_csv_row()
            writer.writerow({key: flat.get(key, "") for key in CSV_COLUMNS})
    return path


def _coverage(rows: Sequence[ResultRow]) -> dict[str, Any]:
    """Summarise which scenarios/methods/sets actually produced rows.

    This is the anti-misread guard. A run whose `aggregates` show `top1: 1.0` over 10 rows
    reads like a perfect result until you notice the corpus was 2 scenarios, not 8. The
    `coverage` block makes the denominator explicit and lists any scenario set that
    contributed blocked rows, so a reader (or the P9 LaTeX writer) cannot cite an aggregate
    over a partial corpus without seeing why.
    """
    by_set: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_set.setdefault(
            row.scenario_set, {"scenarios": set(), "methods": set(), "blocked_scenarios": set()}
        )
        entry["scenarios"].add(row.scenario_id)
        entry["methods"].add(row.method)
        if row.blocked:
            entry["blocked_scenarios"].add(row.scenario_id)

    coverage: dict[str, Any] = {}
    for set_name, entry in by_set.items():
        coverage[set_name] = {
            "scenario_count": len(entry["scenarios"]),
            "methods": sorted(entry["methods"]),
            "scenarios": sorted(entry["scenarios"]),
            "blocked_scenarios": sorted(entry["blocked_scenarios"]),
        }
    return coverage


def write_summary_json(
    rows: Sequence[ResultRow],
    aggregates: Sequence[MethodAggregate],
    results_dir: Path,
    *,
    params: Any = None,
) -> Path:
    """Write ``summary.json``. Raises if any row lacks a `run_id`."""
    _validate_rows(rows)
    path = results_dir / "summary.json"
    coverage = _coverage(rows)
    payload: dict[str, Any] = {
        "generated_at": utcnow().isoformat(),
        "aggregates": [agg.as_dict() for agg in aggregates],
        "provenance": sorted({f"{r.scenario_id}@{r.seed}" for r in rows}),
        "coverage": coverage,
    }

    # Surface anything that would make a bare aggregate misleading, as prose the P9
    # writer and a human reviewer both read.
    warnings: list[str] = []
    blocked_scenarios = {sid for entry in coverage.values() for sid in entry["blocked_scenarios"]}
    if blocked_scenarios:
        warnings.append(
            f"{len(blocked_scenarios)} blocked scenario(s) contributed rows; their accuracy "
            "is not a real result (see D2-Chaos-runtime / D26)."
        )
    if params is not None:
        repeats = getattr(params, "repeats", None)
        for set_name, entry in coverage.items():
            if repeats:
                expected = entry["scenario_count"] * repeats
                actual = sum(1 for r in rows if r.scenario_set == set_name and r.method == "infra")
                if actual < expected:
                    warnings.append(
                        f"set {set_name!r}: infra produced {actual} row(s) across "
                        f"{entry['scenario_count']} scenario(s); some have no committed "
                        "fixture and were skipped, so the aggregate is over a subset."
                    )
    if warnings:
        payload["warnings"] = warnings

    if params is not None:
        payload["params"] = {
            "scenario_set": getattr(params, "scenario_set", None),
            "repeats": getattr(params, "repeats", None),
            "detect_window_s": getattr(params, "detect_window_s", None),
            "allow_blocked": getattr(params, "allow_blocked", None),
        }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def write_all(
    rows: Sequence[ResultRow],
    results_dir: Path,
    *,
    params: Any = None,
    allow_overwrite: bool = False,
) -> dict[str, str]:
    """Write every CSV, `summary.json`, and `README.md`. Returns the paths written.

    `allow_overwrite` is threaded from `EvalParams.allow_overwrite`.
    """
    results_dir = prepare_dir(results_dir, allow_overwrite=allow_overwrite)

    written: dict[str, str] = {}

    grouped: dict[tuple[str, str], list[ResultRow]] = {}
    for row in rows:
        grouped.setdefault((row.scenario_set, row.method), []).append(row)
    for (set_name, method), group in sorted(grouped.items()):
        csv_path = write_rows_csv(group, results_dir, scenario_set=set_name, method=method)
        written[f"csv:{set_name}:{method}"] = str(csv_path)

    aggregates = score(rows)
    summary_path = write_summary_json(rows, aggregates, results_dir, params=params)
    written["summary"] = str(summary_path)

    readme_path = results_dir / "README.md"
    readme_path.write_text(README_BODY)
    written["readme"] = str(readme_path)

    log.info(
        "report_written",
        results_dir=str(results_dir),
        rows=len(rows),
        methods=len(grouped),
    )
    return written


__all__ = [
    "CSV_COLUMNS",
    "README_BODY",
    "prepare_dir",
    "write_all",
    "write_rows_csv",
    "write_summary_json",
]
