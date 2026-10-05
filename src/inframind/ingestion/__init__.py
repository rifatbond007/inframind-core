"""Ingestion: pull signals from external systems and push them onto the event bus.

Planned collectors (Step 3):

* Prometheus — PromQL scrape / query API.
* Loki — query_range API.
* Jaeger / OTLP — trace query API or OTLP receiver.
* Alertmanager — webhook receiver (FastAPI route).

Output: Redis Streams with idempotent consumers.
"""
