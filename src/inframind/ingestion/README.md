# `inframind.ingestion`

Pulls signals from the observability backends and publishes them onto the event bus.

## Collectors

- `prometheus.py` — per-pod CPU, working-set memory and restarts (`PROM_INTERVAL_S`).
- `loki.py` — warning/error log lines, redacted (`LOKI_INTERVAL_S`).
- `jaeger.py` — error spans, per-service p95 latency, and `GraphUpdate` edges on
  `stream:graph-updates` (`JAEGER_INTERVAL_S`).
- `runner.py` — shared entrypoint wrapper (env config, signals, client cleanup).

The Alertmanager webhook is `src/inframind/api/alertmanager.py`. Procedure for a new collector:
`docs/standards/04-add-collector.md`. Live gate: `make smoke-ingestion`.

## Owner

Moneem — ingestion, detection, correlation.
