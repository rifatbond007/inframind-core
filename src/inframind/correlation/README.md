# `inframind.correlation`

Groups anomalies into deduplicated incidents and tracks their lifecycle.

## Will be added in Step 5

- `window.py` — sliding-window grouping.
- `fingerprint.py` — incident fingerprint (deduplication).
- `state_machine.py` — `open -> updating -> resolved`.
- Cycle reconciler — keeps 60s detection and 5-min correlation windows consistent.

## Owner

Moneem — ingestion, detection, correlation.
