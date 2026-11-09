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
    # The harness measures the pipeline, so it may reach the detectors, the tracker, the
    # graph, the ranker, and the explainer — but never the bus-facing P6 layers (alerting,
    # storage, api), or it would be measuring the plumbing rather than the contribution.
    "evaluation": {"common", "correlation", "detection", "rca", "llm"},
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


#: Root modules (`src/inframind/*.py`) sit outside the package table, so they get their own.
#: `main` is the composition root (D28): the one place that may join `rca`, `llm`,
#: `alerting` and `storage` in one process (standard 06b.4). It still may not reach the
#: other stages' internals, the API, or the evaluation harness.
ROOT_ALLOWED: dict[str, set[str]] = {
    "__init__": set(),
    "metrics": {"common"},
    "main": {"common", "rca", "llm", "alerting", "storage"},
}


def test_every_root_module_is_covered_by_the_table() -> None:
    roots = {p.stem for p in SRC.glob("*.py")}
    assert roots <= set(ROOT_ALLOWED), f"add to ROOT_ALLOWED: {sorted(roots - set(ROOT_ALLOWED))}"


@pytest.mark.parametrize("module", sorted(ROOT_ALLOWED))
def test_root_modules_respect_their_allowance(module: str) -> None:
    path = SRC / f"{module}.py"
    siblings = _imports(path, module) & set(ALLOWED)
    forbidden = siblings - ROOT_ALLOWED[module]
    assert not forbidden, (
        f"{module}.py imports {sorted(forbidden)}; allowed: {sorted(ROOT_ALLOWED[module])}"
    )


def test_only_the_composition_root_joins_rca_with_llm_alerting_or_storage() -> None:
    """No package — and no root module other than `main` — may hold the full post-RCA chain."""
    post_rca = {"llm", "alerting", "storage"}
    for package, allowed in ALLOWED.items():
        if package in ("llm", "alerting", "storage", "api", "evaluation"):
            continue  # their own edges are pinned by ALLOWED above
        assert not (allowed & post_rca), f"{package} may not reach {sorted(allowed & post_rca)}"
    joined = [m for m, allowed in ROOT_ALLOWED.items() if {"rca", "storage"} <= allowed]
    assert joined == ["main"], joined
