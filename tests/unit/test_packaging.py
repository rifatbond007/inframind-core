"""Every non-Python file the package reads at runtime must ship in the wheel.

Unit tests run against the editable source tree, where every file is present, so a data
file left out of `[tool.setuptools.package-data]` only fails inside the Docker image. That
is how the LLM prompt template went missing and every live summary silently became the
template fallback. This test pins the package-data globs to the files on disk.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
_IGNORED_SUFFIXES = {".py", ".pyc", ".md"}


def _package_data() -> dict[str, list[str]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return data["tool"]["setuptools"].get("package-data", {})


def _covered(path: Path, globs: dict[str, list[str]]) -> bool:
    for package, patterns in globs.items():
        package_dir = SRC / Path(*package.split("."))
        try:
            relative = path.relative_to(package_dir)
        except ValueError:
            continue
        if any(fnmatch.fnmatch(relative.as_posix(), pattern) for pattern in patterns):
            return True
    return False


def test_every_runtime_data_file_is_declared_as_package_data() -> None:
    globs = _package_data()
    data_files = [
        p
        for p in (SRC / "inframind").rglob("*")
        if p.is_file() and p.suffix not in _IGNORED_SUFFIXES and "__pycache__" not in p.parts
    ]
    assert data_files, "expected at least the LLM prompt template"
    missing = [str(p.relative_to(SRC)) for p in data_files if not _covered(p, globs)]
    assert not missing, f"add to [tool.setuptools.package-data]: {missing}"


def test_the_llm_prompt_template_is_declared() -> None:
    prompt = SRC / "inframind" / "llm" / "prompts" / "rca_summary.v1.j2"
    assert prompt.is_file()
    assert _covered(prompt, _package_data())
