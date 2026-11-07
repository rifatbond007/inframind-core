"""Correlation parameters and their `INFRAMIND_CORRELATION_<PARAM>` overrides.

Defaults come from the P4b design; they are starting values, tuned on the dev split in P7 (D6).
`INFRAMIND_CORRELATION_ENABLED=false` is the D7 `no-correlation` ablation: every anomaly then
opens its own incident.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from inframind.detection.config import load_prefixed


@dataclass(frozen=True)
class CorrelationParams:
    """Grouping window, lifecycle timers, severity thresholds and the ablation switch."""

    window_s: float = 300.0
    cooldown_s: float = 120.0
    max_age_s: float = 3600.0
    update_interval_s: float = 30.0
    critical_services: int = 3
    critical_anomalies: int = 20
    enabled: bool = True


def load_correlation_params(env: Mapping[str, str] | None = None) -> CorrelationParams:
    """Build `CorrelationParams` from the environment (`os.environ` when `env` is None)."""
    return load_prefixed(CorrelationParams, "INFRAMIND_CORRELATION", env)
