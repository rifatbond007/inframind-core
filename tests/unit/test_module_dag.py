"""The module dependency DAG of standards 01 section 1.3, enforced on the real source tree."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "inframind"

ALLOWED: dict[str, set[str]] = {
    "common": set(),
    "ingestion": {"common"},
    "detection": {"common"},
    "correlation": {"common", "detection"},
    "rca": {"common"},
    "llm": {"common", "rca"},
    "alerting": {"common", "rca", "llm"},
    "storage": {"common", "correlation", "rca"},
    "api": {"common", "storage", "alerting"},
}


def _imports(path: Path, package: str) -> set[str]:
    """Return the sibling InfraMind packages that `path` imports (absolute or relative)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level >= 2:  # `from ..other import x` reaches a sibling package
                found.add((node.module or "").split(".")[0])
            elif node.level == 0 and (node.module or "").startswith("inframind."):
                found.add(node.module.split(".")[1])
            elif node.level == 0 and node.module == "inframind":
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(
                a.name.split(".")[1] for a in node.names if a.name.startswith("inframind.")
            )
    return found - {package, ""}


def _package_files() -> list[tuple[str, Path]]:
    return [(pkg, f) for pkg in ALLOWED for f in sorted((SRC / pkg).rglob("*.py"))]


@pytest.mark.parametrize(("package", "path"), _package_files(), ids=lambda v: str(v))
def test_module_imports_respect_the_dag(package: str, path: Path) -> None:
    siblings = _imports(path, package) & set(ALLOWED)
    forbidden = siblings - ALLOWED[package]
    assert not forbidden, (
        f"{path.relative_to(SRC)} imports {sorted(forbidden)}; allowed: {sorted(ALLOWED[package])}"
    )


def test_every_source_package_is_covered_by_the_table() -> None:
    packages = {p.name for p in SRC.iterdir() if p.is_dir() and not p.name.startswith("__")}
    assert packages <= set(ALLOWED), f"add to ALLOWED: {sorted(packages - set(ALLOWED))}"


def test_only_api_imports_fastapi() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.relative_to(SRC).parts[0] == "api":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            if any(n == "fastapi" or n.startswith("fastapi.") for n in names):
                offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders
