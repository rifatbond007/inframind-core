"""Report tests: provenance enforcement and overwrite refusal."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from inframind.evaluation.report import (
    CSV_COLUMNS,
    prepare_dir,
    write_all,
    write_rows_csv,
    write_summary_json,
)
from inframind.evaluation.schema import ResultRow


def _row(**overrides: object) -> ResultRow:
    base = {
        "scenario_id": "dev-x",
        "seed": 1001,
        "repeat": 0,
        "method": "infra",
        "method_kind": "infra",
        "detected": True,
        "top1_correct": True,
        "top3_correct": True,
        "reciprocal_rank": 1.0,
        "rank": 1,
        "detection_latency_s": 42.0,
        "false_positive": False,
        "scenario_set": "dev",
        "run_id": "dev-x@1001r0:infra",
        "expected_root_cause": "cartservice",
        "predicted_root_cause": "cartservice",
    }
    base.update(overrides)
    return ResultRow(**base)  # type: ignore[arg-type]


def test_write_rows_csv_has_the_documented_header(tmp_path: Path) -> None:
    """The CSV header matches `CSV_COLUMNS` exactly, in order."""
    path = write_rows_csv([_row()], tmp_path, scenario_set="dev", method="infra")
    with path.open() as fh:
        header = next(csv.reader(fh))
    assert tuple(header) == CSV_COLUMNS


def test_write_rows_csv_renders_none_as_empty(tmp_path: Path) -> None:
    """A None latency is an empty CSV cell, not the string "None"."""
    row = _row(detection_latency_s=None, predicted_root_cause=None)
    path = write_rows_csv([row], tmp_path, scenario_set="dev", method="infra")
    with path.open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["detection_latency_s"] == ""
    assert rows[0]["predicted_root_cause"] == ""


def test_rows_without_run_id_are_rejected(tmp_path: Path) -> None:
    """A row with no `run_id` cannot be written (PUKU.md: never invent a number)."""
    row = _row(run_id="")
    with pytest.raises(ValueError, match="no run_id"):
        write_rows_csv([row], tmp_path, scenario_set="dev", method="infra")
    with pytest.raises(ValueError, match="no run_id"):
        write_summary_json([row], [], tmp_path)


def test_prepare_dir_refuses_a_populated_directory(tmp_path: Path) -> None:
    """An existing, non-empty results directory is not clobbered by default."""
    results = tmp_path / "phase7"
    results.mkdir()
    (results / "summary.json").write_text("{}")
    with pytest.raises(FileExistsError, match="already has content"):
        prepare_dir(results)


def test_prepare_dir_allows_overwrite_with_the_flag(tmp_path: Path) -> None:
    """`allow_overwrite=True` is the deliberate-act escape hatch."""
    results = tmp_path / "phase7"
    results.mkdir()
    (results / "summary.json").write_text("{}")
    returned = prepare_dir(results, allow_overwrite=True)
    assert returned == results


def test_prepare_dir_creates_a_missing_directory(tmp_path: Path) -> None:
    """A missing results directory is created."""
    results = tmp_path / "nested" / "phase7"
    returned = prepare_dir(results)
    assert returned.exists()
    assert returned.is_dir()


def test_write_all_emits_one_csv_per_method_plus_summary(tmp_path: Path) -> None:
    """`write_all` groups CSVs by (set, method) and writes summary.json + README."""
    rows = [
        _row(method="infra"),
        _row(method="raw_alertmanager", method_kind="baseline"),
        _row(scenario_set="test", method="infra", run_id="dev-x@1001r0:infra"),
    ]
    written = write_all(rows, tmp_path / "results")
    assert "csv:dev:infra" in written
    assert "csv:dev:raw_alertmanager" in written
    assert "csv:test:infra" in written
    assert "summary" in written
    assert "readme" in written


def test_summary_json_carries_aggregates_and_provenance(tmp_path: Path) -> None:
    """`summary.json` records the aggregates plus the (scenario, seed) provenance."""
    rows = [_row(), _row(method="infra", repeat=1, run_id="dev-x@1001r1:infra")]
    path = write_summary_json(rows, [], tmp_path)
    payload = json.loads(path.read_text())
    assert "generated_at" in payload
    assert "aggregates" in payload
    assert payload["provenance"] == ["dev-x@1001"]
    assert payload["coverage"]["dev"]["scenario_count"] == 1
    assert "infra" in payload["coverage"]["dev"]["methods"]


def test_summary_json_warns_when_a_blocked_row_was_scored(tmp_path: Path) -> None:
    """A `blocked=True` row in `coverage` triggers a warning string in the summary."""
    rows = [_row(blocked=True)]
    path = write_summary_json(rows, [], tmp_path)
    payload = json.loads(path.read_text())
    assert any("blocked" in w for w in payload["warnings"])


def test_summary_json_warns_when_infra_undercounts_a_scenario_set(tmp_path: Path) -> None:
    """When `infra` produced fewer rows than `scenario_count * repeats`, the summary says so.

    This is the mechanical guard against citing a `top1: 1.0` aggregate as if it were a
    full D6 matrix when some scenarios have no committed fixture. P9's `results-to-latex`
    reader will know to prefix the row.
    """

    class _Params:
        scenario_set = "dev"
        repeats = 5
        detect_window_s = 120.0
        allow_blocked = False

    rows = [_row()]  # 1 infra row, but the params say repeats=5 and there's only 1 scenario
    path = write_summary_json(rows, [], tmp_path, params=_Params())
    payload = json.loads(path.read_text())
    assert any("subset" in w for w in payload["warnings"])


def test_write_all_records_the_params(tmp_path: Path) -> None:
    """`summary.json` echoes the run params so a number is reproducible from context."""

    class _Params:
        scenario_set = "dev"
        repeats = 5
        detect_window_s = 120.0
        allow_blocked = False

    write_all([_row()], tmp_path / "results", params=_Params())
    payload = json.loads((tmp_path / "results" / "summary.json").read_text())
    assert payload["params"]["scenario_set"] == "dev"
    assert payload["params"]["repeats"] == 5


def test_write_all_refuses_to_overwrite_by_default(tmp_path: Path) -> None:
    """Two runs into the same directory need `allow_overwrite=True`."""
    results = tmp_path / "results"
    write_all([_row()], results)
    with pytest.raises(FileExistsError):
        write_all([_row()], results)
    write_all([_row()], results, allow_overwrite=True)


def test_readme_states_the_no_hand_written_numbers_rule(tmp_path: Path) -> None:
    """The committed README is written into the results directory on every run."""
    results = tmp_path / "results"
    write_all([_row()], results)
    readme = (results / "README.md").read_text()
    assert "written by hand" in readme
    assert "run_id" in readme
