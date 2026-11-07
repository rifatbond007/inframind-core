# `inframind.correlation`

Groups anomalies into incidents and tracks their lifecycle.

## Built (P4, minimal)

- `tracker.py` — `IncidentTracker`: one open incident per service; opens on the first anomaly
  (and freezes the baselines), resolves after a quiet cooldown (and unfreezes).
- `worker.py` — `CorrelationWorker`: `stream:signals` in, detection and tracker, `stream:incidents`
  out. Run with `python -m inframind.correlation.worker`.

## Will be added in P4b

- `window.py` — sliding-window grouping across services.
- `fingerprint.py` — incident fingerprint (deduplication).
- `state_machine.py` — the full `open -> acknowledged -> resolved/expired` lifecycle.

One burst on the frontend lifted CPU on six services and opened six incidents; grouping them is
the first job of P4b.

## Owner

Moneem — ingestion, detection, correlation.
