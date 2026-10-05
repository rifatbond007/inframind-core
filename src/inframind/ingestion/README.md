# `inframind.ingestion`

Pulls signals from observability backends and pushes them onto the event bus.

## Will be added in Step 3

- `prometheus.py` — Prometheus collector (PromQL).
- `loki.py` — Loki collector (query_range).
- `jaeger.py` — Jaeger / OTLP collector.
- `alertmanager_webhook.py` — FastAPI webhook for Alertmanager notifications.
- `bus.py` — Redis Streams publisher with idempotent consumers.

## Owner

Moneem — ingestion, detection, correlation.
