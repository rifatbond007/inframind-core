---
name: add-detector
description: How to add a new anomaly detector (Z-score, EWMA, error spike, latency regression) in InfraMind.
---

# Add a detector (skill entrypoint)

**Canonical procedure:** [`docs/standards/05-add-detector.md`](../../../docs/standards/05-add-detector.md)

Before adding a detector:

1. Read [`docs/PROGRESS.md`](../../../docs/PROGRESS.md) — detection is Phase P4.
2. Honour baseline freeze (D4) via `freeze()` / `unfreeze()` on the detector API.
3. Log the session in `docs/PROGRESS.md` when done.

Owner: detection-engineer agent (`Moneem`).
