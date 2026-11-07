"""Ingestion: pull signals from the observability backends and publish them to the event bus.

Collectors (each runs with ``python -m inframind.ingestion.<name>``):

* ``prometheus`` — per-pod CPU, memory and restarts from ``query_range``.
* ``loki`` — warning/error log lines from ``query_range``.
* ``jaeger`` — error spans, per-service latency, and ``GraphUpdate`` call edges.

``runner`` is the shared process wrapper. The Alertmanager webhook is the only push ingress; it
lives in ``inframind.api.alertmanager`` because only ``api`` may import FastAPI.
"""
