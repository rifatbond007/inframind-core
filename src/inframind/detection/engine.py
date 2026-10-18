"""DetectionEngine: runs every detector over each signal and keeps the freeze leases honest.

`process` is the single entry point of the detection stage: it first releases freeze leases
that expired by the signal's own timestamp, then lets each detector score the signal. Each
detector ignores the signals it does not model, so routing is just iteration in a fixed order
(`zscore`, `ewma`, `error_spike`, `latency_regression`), which keeps replays deterministic (D6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Signal

from .base import Detector
from .config import (
    ErrorSpikeParams,
    EwmaParams,
    LatencyParams,
    ZScoreParams,
    freeze_window_s,
    load_params,
)
from .error_spike import ErrorSpikeDetector
from .ewma import EwmaDetector
from .freeze import BaselineFreezeCoordinator
from .latency_regression import LatencyRegressionDetector
from .zscore import ZScoreDetector


class DetectionEngine:
    """The four detectors plus the coordinator that freezes their baselines."""

    def __init__(
        self, detectors: Sequence[Detector], coordinator: BaselineFreezeCoordinator
    ) -> None:
        self._detectors = list(detectors)
        self._coordinator = coordinator

    @classmethod
    def default(cls, env: Mapping[str, str] | None = None) -> DetectionEngine:
        """Build the standard engine; `INFRAMIND_DETECTION_*` and `BASELINE_FREEZE_WINDOW` apply."""
        detectors: list[Detector] = [
            ZScoreDetector(load_params(ZScoreParams, "zscore", env)),
            EwmaDetector(load_params(EwmaParams, "ewma", env)),
            ErrorSpikeDetector(load_params(ErrorSpikeParams, "error_spike", env)),
            LatencyRegressionDetector(load_params(LatencyParams, "latency_regression", env)),
        ]
        return cls(detectors, BaselineFreezeCoordinator(detectors, freeze_window_s(env)))

    @property
    def coordinator(self) -> BaselineFreezeCoordinator:
        """The freeze coordinator the incident tracker drives."""
        return self._coordinator

    @property
    def detectors(self) -> tuple[Detector, ...]:
        """The detectors, in evaluation order."""
        return tuple(self._detectors)

    def process(self, signal: Signal) -> list[Anomaly]:
        """Release expired leases, then return the anomalies every detector reports."""
        self._coordinator.expire(signal.ts)
        found = (detector.update(signal) for detector in self._detectors)
        return [anomaly for anomaly in found if anomaly is not None]
