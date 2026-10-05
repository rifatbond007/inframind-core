---
name: add-detector
description: How to add a new anomaly detector (Z-score, EWMA, error spike, latency regression) to InfraMind's detection layer.
---

# Add a detector

Use this skill when adding a new statistical anomaly detector under `src/inframind/detection/`.

## Pre-flight

1. Read `docs/PROGRESS.md` (Phase 4).
2. Read `src/inframind/detection/README.md`.
3. Read `src/inframind/common/README.md` — the `Signal` contract.

## Steps

1. Create `src/inframind/detection/<detector>.py`.
2. Implement a class with:
   - `def update(self, signal: Signal) -> Anomaly | None` — incremental, per-service stateful.
   - `def freeze(self) -> None` — called when an incident opens (D4).
   - `def unfreeze(self) -> None` — called when the incident resolves.
3. State is per-service. Rolling windows or EWMA, not global.
4. Honour `INFRAMIND_DETECTION_*` env vars for window size and thresholds.

## Tests

- `tests/unit/detection/test_<detector>.py` — synthetic time-series with a known spike, assert the detector fires.
- `tests/integration/test_<detector>_live.py` — against a live Prometheus / Loki / Jaeger.

## Tuning

- Use the **dev** split of scenarios for threshold tuning.
- Report on the **test** split. Never tune on the test set.

## Commit

- `feat(detection): add <detector> detector`
- Update `docs/PROGRESS.md` with the new row.
- Update `docs/ARCHITECTURE.md` detection section if a new algorithm is added.