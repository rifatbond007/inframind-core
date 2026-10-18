# `inframind.common`

Shared cross-cutting code used by every other module. Imports nothing from InfraMind.

- `signal.py` — `Signal`, its enums, `normalise_service`, `service_from_pod`.
- `graph_update.py` — `GraphUpdate`, the call-edge sample on `stream:graph-updates` (D21).
- `bus.py` — `publish`, `publish_graph_update`, and the idempotent `SignalConsumer`.
- `anomaly.py` / `incident.py` — the `Anomaly` and `Incident` wire models (`stream:incidents`).
- `collector_state.py` — cursor and seen-key state that survives collector restarts.
- `redact.py` — secret/PII redaction applied before text reaches the bus or an LLM (D9).
- `stream_keys.py`, `ids.py`, `time.py`, `logging.py`, `exceptions.py` — pinned names and helpers.

`Config` (env-driven settings) lands with the first service that needs it.
