# 5. Add a detector — Z-score / EWMA / error-rate / latency
> standards · Python conventions · InfraMind. Pairs with the `add-detector` skill in `.claude/skills/add-detector/SKILL.md`. This file is the rationale and the worked Z-score example.

## 5.1 The shape every detector has

```
src/inframind/detection/<detector>.py
```

```python
from inframind.common.signal import Signal
from inframind.detection.anomaly import Anomaly


class <Name>Detector:
    """Statistical <method> detector. Stateful per service."""

    def __init__(self, ...) -> None: ...
    def update(self, signal: Signal) -> Anomaly | None: ...
    def freeze(self) -> None: ...
    def unfreeze(self) -> None: ...
```

A detector is a long-lived object. It owns per-service state — rolling windows, EWMA
coefficients, or whatever the algorithm needs.

## 5.2 Three methods, three responsibilities

- **`def update(self, signal: Signal) -> Anomaly | None`** — incremental. Called for every
  signal. Returns `None` most of the time; returns an `Anomaly` when the detector fires.
- **`def freeze(self)`** — called when an incident opens on a service. The detector stops
  updating its baseline for that service. D4.
- **`def unfreeze(self)`** — called when the incident resolves. The detector resumes updating
  its baseline.

The freeze/unfreeze lifecycle is what makes the system correct under a sustained fault.
Without it, the fault slowly becomes "normal" and the detector silently stops firing.

## 5.3 The state discipline

- **State is per-service.** A global rolling window is wrong — the noise floor differs across
  services, and a global window lets a noisy service silence a quiet one.
- **State is in-memory.** A restart loses the rolling window. That is acceptable: the next
  window fills in `interval * window_size` seconds.
- **State is private.** No module outside `detection/` touches a detector's state.

## 5.4 Worked example: a Z-score detector

```python
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from inframind.common.signal import Signal
from inframind.detection.anomaly import Anomaly
from inframind.detection.severity import severity_from_zscore


@dataclass
class _ServiceState:
    """Per-service rolling state."""
    window: Deque[float] = field(default_factory=deque)
    frozen: bool = False
    mean: float = 0.0
    std: float = 0.0


class ZScoreDetector:
    """Per-service rolling Z-score on a numeric attribute. Default window 60, threshold 3.0."""

    def __init__(self, window: int = 60, threshold: float = 3.0,
                 attr: str = "value") -> None:
        self._window = window
        self._threshold = threshold
        self._attr = attr
        self._state: dict[str, _ServiceState] = {}

    def update(self, signal: Signal) -> Anomaly | None:
        if signal.type.name != "METRIC":
            return None
        value = (signal.attrs or {}).get(self._attr)
        if not isinstance(value, (int, float)):
            return None

        st = self._state.setdefault(signal.service, _ServiceState())
        if st.frozen:
            return None  # D4: do not update the baseline while an incident is open

        st.window.append(float(value))
        if len(st.window) > self._window:
            st.window.popleft()
        if len(st.window) < 5:  # warm-up
            return None

        st.mean = sum(st.window) / len(st.window)
        variance = sum((x - st.mean) ** 2 for x in st.window) / len(st.window)
        st.std = math.sqrt(variance) or 1e-9
        z = (value - st.mean) / st.std

        if abs(z) >= self._threshold:
            return Anomaly(
                detector="zscore",
                service=signal.service,
                signal_id=signal.id,
                score=z,
                severity=severity_from_zscore(abs(z)),
                attrs={"value": value, "mean": st.mean, "std": st.std, "window": len(st.window)},
            )
        return None

    def freeze(self, service: str) -> None:
        if service in self._state:
            self._state[service].frozen = True

    def unfreeze(self, service: str) -> None:
        if service in self._state:
            self._state[service].frozen = False
```

## 5.5 The Anomaly shape

```python
class Anomaly(BaseModel):
    detector: str           # "zscore", "ewma", "error_rate", "trace_p95"
    service: str
    signal_id: str          # back-reference to the source Signal
    score: float            # raw detector score (z, EWMA residual, etc.)
    severity: str           # refined severity from the detector
    attrs: dict[str, Any]   # detector-specific context
```

The correlation stage (`correlation/`) consumes `Anomaly` objects and groups them into
incidents. The detector is done when it has produced (or not produced) the `Anomaly`.

## 5.6 The freeze discipline (D4)

- **`freeze()`** is called by the correlation stage when a service's `Anomaly` has been
  promoted into an incident. Until that call, the baseline keeps updating.
- **`unfreeze()`** is called when the incident transitions to `resolved` or after the
  configured `BASELINE_FREEZE_WINDOW` expires. The latter is a safety net — if the system
  forgets to call `unfreeze`, the detector still resumes after the window.
- A frozen detector still emits `Anomaly` objects; it just stops *updating the baseline*. A
  fault can therefore keep firing — which is the point.

## 5.7 Tuning protocol

- **Dev split is for tuning.** Thresholds are chosen on `evaluation/scenarios/dev/`. The
  optimisation target is the F1 / MTTD trade-off the team agrees on.
- **Test split is for the report.** Numbers in the paper come from `evaluation/scenarios/test/`
  only. Tuning on the test set is a paper-disqualifying error.
- **No fit on production.** The dev split is `evaluation/scenarios/dev/`; the test split is
  `evaluation/scenarios/test/`. The split is locked at the start of P7 and never re-shuffled.

## 5.8 Tests

```
tests/unit/detection/test_zscore.py
tests/integration/test_zscore_live.py
```

- The unit test feeds a synthetic time-series with a known spike and asserts the detector
  fires with the expected z-score.
- The integration test runs the detector against a live Prometheus (in the testbed) and asserts
  the same on a Chaos-Mesh-injected fault.

## 5.9 Hard rules

- **State is per-service.** No global rolling windows.
- **Freeze on incident open, unfreeze on resolve.** D4. A detector that ignores freeze is
  broken even if it scores perfectly on dev.
- **Tune on dev, report on test.** D6. The two splits are sacred.
- **No DL in the detector.** D3. Statistical only.
- **Tunable parameters via env vars.** `INFRAMIND_DETECTION_<DETECTOR>_<PARAM>`. Never via
  constructor args from a module import — the operator must be able to retune without
  redeploying code.

## 5.10 Cross-reference

- **Skill:** `.claude/skills/add-detector/SKILL.md`.
- **Locked decisions:** D4 (baseline freeze), D6 (dev/test split), D3 (no DL).
- **Next stage:** `correlation/` consumes the `Anomaly` and groups them. The freeze lifecycle
  is wired in `correlation/state.py`.
- **Evaluation:** `10-evaluation.md` in this set — how the tuned thresholds become numbers.
