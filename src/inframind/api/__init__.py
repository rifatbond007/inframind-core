"""The HTTP API. Only this package may import FastAPI.

Built:

* ``GET /healthz`` — liveness probe (P3, unchanged).
* ``POST /webhooks/alertmanager`` — the only push ingress (P3, unchanged).
* ``GET /readyz`` — readiness probe, checks Redis and Postgres (P6).
* ``GET /metrics`` — minimal Prometheus exposition for the API process itself (P6).
* ``GET /incidents`` — paginated list with filters (P6).
* ``GET /incidents/{id}`` — full incident with ranked candidates and evidence (P6).
* ``POST /incidents/{id}/ack`` — transition with D25 `X-Actor` (P6).
* ``POST /incidents/{id}/resolve`` — transition with D25 `X-Actor` (P6).

DAG: the API is the only module allowed to import FastAPI. State transitions require
the `X-Actor` header to be a GitHub username (D25 — AI tool names are rejected at the
boundary).
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
