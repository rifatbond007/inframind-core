"""Shared fixtures for the evaluation test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_eval_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test sees an isolated results/fixture directory.

    Defaults the harness reads from the environment (`INFRAMIND_EVAL_*`) — `EvalParams`
    are rebuilt per call, so a tmp_path fixture is enough to keep tests independent.
    """
    monkeypatch.setenv("INFRAMIND_EVAL_FIXTURE_DIR", str(tmp_path / "fixtures"))
    monkeypatch.setenv("INFRAMIND_EVAL_RESULTS_DIR", str(tmp_path / "results" / "phase7"))
    yield


@pytest.fixture
def repo_paths() -> tuple[Path, Path]:
    """Return `(scenarios_root, fixtures_root)` pointing at the real repo locations."""
    # Path is: tests/unit/evaluation/conftest.py -> four `.parents` to the repo root.
    repo = Path(__file__).resolve().parents[3]
    return repo / "evaluation" / "scenarios", repo / "evaluation" / "fixtures"
