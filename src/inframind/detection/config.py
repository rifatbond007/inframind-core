"""Detector parameters, per-metric noise profiles, and their environment overrides.

Tunables follow standard 05 §5.9: `INFRAMIND_DETECTION_<DETECTOR>_<PARAM>`. The baseline freeze
lease keeps its documented key `BASELINE_FREEZE_WINDOW` (surface-map); the incident cooldown
is `INFRAMIND_CORRELATION_COOLDOWN_S`. Defaults come from the P4 design; they are starting
values, tuned on the dev split in P7 (D6) and never on the test split.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeVar

from inframind.common.config import _seconds, load_prefixed

T = TypeVar("T")

# `load_prefixed`, `_cast` and `_seconds` now live in `common/config.py` so the `rca` layer can
# use them without importing `detection` (module DAG). They are re-exported here because the
# detection tests and the repo-metadata test import them from this module.


@dataclass(frozen=True)
class MetricProfile:
    """Noise floors for one metric so a flat series cannot make a tiny wiggle look huge.

    `direction` says which deviations are anomalies: `"up"` for resource gauges and counters,
    where a drop means the service went idle (traffic stopped) and is not a fault; `"both"` for
    metrics nobody has profiled yet.
    """

    abs_floor: float
    rel_floor: float
    counter: bool = False
    direction: Literal["up", "both"] = "both"


_PROFILES: dict[str, MetricProfile] = {
    "cpu_cores": MetricProfile(abs_floor=0.005, rel_floor=0.10, direction="up"),
    "memory_working_set_bytes": MetricProfile(abs_floor=8 * 2**20, rel_floor=0.05, direction="up"),
    "container_restarts_total": MetricProfile(
        abs_floor=0.25, rel_floor=0.0, counter=True, direction="up"
    ),
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
    return load_prefixed(cls, f"INFRAMIND_DETECTION_{detector.upper()}", env)


def freeze_window_s(env: Mapping[str, str] | None = None) -> float:
    """Return the baseline freeze lease in seconds (`BASELINE_FREEZE_WINDOW`, default 300)."""
    return _seconds("BASELINE_FREEZE_WINDOW", 300.0, env)


def cooldown_s(env: Mapping[str, str] | None = None) -> float:
    """Return the quiet time before an incident resolves (`INFRAMIND_CORRELATION_COOLDOWN_S`)."""
    return _seconds("INFRAMIND_CORRELATION_COOLDOWN_S", 120.0, env)
