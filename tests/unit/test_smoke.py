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


def test_main_version_flag_exits_zero(capsys) -> None:  # type: ignore[no-untyped-def]
    """`python -m inframind.main --version` prints the version without touching a backend."""
    import inframind
    from inframind.main import main

    assert main(["--version"]) == 0
    assert inframind.__version__ in capsys.readouterr().out
