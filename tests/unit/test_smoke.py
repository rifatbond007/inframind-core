"""Smoke test — proves the package is importable and the version is set.

This is the only test that runs in Step 0. Real test coverage lands phase by phase.
"""

from __future__ import annotations


def test_inframind_importable() -> None:
    """The package imports cleanly under the pinned Python version."""
    import inframind

    assert hasattr(inframind, "__version__")
    assert isinstance(inframind.__version__, str)
    assert inframind.__version__  # non-empty


def test_main_module_runs() -> None:
    """The placeholder entrypoint exits with status 0."""
    from inframind.main import main

    assert main() == 0
