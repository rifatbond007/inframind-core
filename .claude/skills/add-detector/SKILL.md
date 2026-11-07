---
name: add-detector
description: How to add a new anomaly detector (Z-score, EWMA, error spike, latency regression) in InfraMind.
---

# Add a detector (skill entrypoint)

**Canonical procedure:** [`docs/standards/05-add-detector.md`](../../../docs/standards/05-add-detector.md)

Before adding a detector:

1. Read [`docs/PROGRESS.md`](../../../docs/PROGRESS.md) — detection is Phase P4.
2. Implement the `Detector` protocol (`update`, `freeze(service, ts)`, `unfreeze`, `is_frozen`); honour baseline freeze (D4): a frozen detector keeps scoring and only stops learning.
3. Use the per-metric noise floors and direction in `detection/config.py`; take time from `Signal.ts`; make redelivery a no-op.
4. Prove it with a synthetic-series unit test, keep `tests/unit/detection/test_replay.py` green, and run `make smoke-detection`.
5. Log the session in `docs/PROGRESS.md` when done.

Owner: detection-engineer agent (`Moneem`).
