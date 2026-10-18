"""Detector parameters, per-metric noise profiles, and their environment overrides.

Tunables follow standard 05 §5.9: `INFRAMIND_DETECTION_<DETECTOR>_<PARAM>`. The baseline freeze
lease keeps its documented key `BASELINE_FREEZE_WINDOW` (surface-map); the incident cooldown
is `INFRAMIND_CORRELATION_COOLDOWN_S`. Defaults come from the P4 design; they are starting
values, tuned on the dev split in P7 (D6) and never on the test split.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeVar, get_type_hints

T = TypeVar("T")


@dataclass(frozen=True)
class MetricProfile:
    """Noise floors for one metric so a flat series cannot make a tiny wiggle look huge."""

    abs_floor: float
    rel_floor: float
    counter: bool = False


_PROFILES: dict[str, MetricProfile] = {
    "cpu_cores": MetricProfile(abs_floor=0.005, rel_floor=0.10),
    "memory_working_set_bytes": MetricProfile(abs_floor=8 * 2**20, rel_floor=0.05),
    "container_restarts_total": MetricProfile(abs_floor=0.25, rel_floor=0.0, counter=True),
}
_DEFAULT_PROFILE = MetricProfile(abs_floor=0.0, rel_floor=0.10)


def profile_for(metric: str) -> MetricProfile:
    """Return the noise profile for `metric` (a relative-only floor when it is unknown)."""
    return _PROFILES.get(metric, _DEFAULT_PROFILE)


@dataclass(frozen=True)
class ZScoreParams:
    """Rolling Z-score: `window` samples, fire at `threshold` sigmas after `min_samples`."""

    window: int = 60
    threshold: float = 3.0
    min_samples: int = 10


@dataclass(frozen=True)
class EwmaParams:
    """Exponentially weighted mean/variance: smoothing `alpha`, fire beyond `sigma_k` sigmas."""

    alpha: float = 0.3
    sigma_k: float = 3.0
    min_samples: int = 10


@dataclass(frozen=True)
class ErrorSpikeParams:
    """Error-event count spike over a sliding `window_s` against a `baseline_windows` baseline."""

    window_s: int = 300
    min_count: int = 3
    k: float = 3.0
    baseline_windows: int = 12


@dataclass(frozen=True)
class LatencyParams:
    """p95 regression: `regression_pct` over the rolling median, `consecutive` polls in a row."""

    window: int = 60
    regression_pct: float = 50.0
    min_delta_ms: float = 10.0
    min_spans: int = 10
    consecutive: int = 2
    min_samples: int = 10


def load_params(cls: type[T], detector: str, env: Mapping[str, str] | None = None) -> T:
    """Build `cls` from its defaults, overridden by `INFRAMIND_DETECTION_<DETECTOR>_<FIELD>`.

    Raises:
        ValueError: an override is set but cannot be cast to the field's type; the message
            names the environment variable.
    """
    source = os.environ if env is None else env
    hints = get_type_hints(cls)
    overrides: dict[str, object] = {}
    for field in dataclasses.fields(cls):  # type: ignore[arg-type]
        name = f"INFRAMIND_DETECTION_{detector.upper()}_{field.name.upper()}"
        if name not in source:
            continue
        try:
            overrides[field.name] = hints[field.name](source[name])
        except ValueError as e:
            raise ValueError(
                f"{name}={source[name]!r} is not a valid {hints[field.name].__name__}"
            ) from e
    return cls(**overrides)


def _seconds(name: str, default: float, env: Mapping[str, str] | None) -> float:
    source = os.environ if env is None else env
    if name not in source:
        return default
    try:
        return float(source[name])
    except ValueError as e:
        raise ValueError(f"{name}={source[name]!r} is not a number of seconds") from e


def freeze_window_s(env: Mapping[str, str] | None = None) -> float:
    """Return the baseline freeze lease in seconds (`BASELINE_FREEZE_WINDOW`, default 300)."""
    return _seconds("BASELINE_FREEZE_WINDOW", 300.0, env)


def cooldown_s(env: Mapping[str, str] | None = None) -> float:
    """Return the quiet time before an incident resolves (`INFRAMIND_CORRELATION_COOLDOWN_S`)."""
    return _seconds("INFRAMIND_CORRELATION_COOLDOWN_S", 120.0, env)
