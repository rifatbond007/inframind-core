"""Shared environment-overload helpers for the `INFRAMIND_<LAYER>_<FIELD>` config pattern.

Several layers (`detection`, `correlation`, `rca`) each own a frozen dataclass of tunables that
may be overridden from the environment. They all need the same two primitives: build a
dataclass from its defaults plus `<PREFIX>_<FIELD>` overrides (`load_prefixed`), and read a
duration in seconds from a named variable (`_seconds`).

These helpers are deliberately dependency-free and live in `common/` so any layer may import
them without violating the module DAG (`tests/unit/test_module_dag.py` enforces the allowed
imports per package). `detection/config.py` was the original home, but `rca` may only import
`common`, so the shared pieces were lifted here; detection re-exports them for compatibility.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from typing import TypeVar, get_type_hints

T = TypeVar("T")


def _cast(hint: type, raw: str) -> object:
    """Coerce the environment string `raw` to the field type `hint`, with bool spelled out."""
    if hint is bool:
        lowered = raw.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise ValueError(f"{raw!r} is not a boolean (use true/false/1/0)")
    return hint(raw)


def load_prefixed(cls: type[T], prefix: str, env: Mapping[str, str] | None = None) -> T:
    """Build dataclass `cls` from its defaults, overridden by `<PREFIX>_<FIELD>` variables.

    Raises:
        ValueError: an override is set but cannot be cast to the field's type; the message
            names the environment variable.
    """
    source = os.environ if env is None else env
    hints = get_type_hints(cls)
    overrides: dict[str, object] = {}
    for field in dataclasses.fields(cls):  # type: ignore[arg-type]
        name = f"{prefix}_{field.name.upper()}"
        if name not in source:
            continue
        try:
            overrides[field.name] = _cast(hints[field.name], source[name])
        except ValueError as e:
            raise ValueError(
                f"{name}={source[name]!r} is not a valid {hints[field.name].__name__}"
            ) from e
    return cls(**overrides)


def _seconds(name: str, default: float, env: Mapping[str, str] | None) -> float:
    """Read a float-seconds variable `name`, falling back to `default` when unset."""
    source = os.environ if env is None else env
    if name not in source:
        return default
    try:
        return float(source[name])
    except ValueError as e:
        raise ValueError(f"{name}={source[name]!r} is not a number of seconds") from e
