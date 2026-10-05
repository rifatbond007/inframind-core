"""Public API surface (FastAPI).

Planned (Step 8):

* ``/healthz`` — liveness probe.
* ``/readyz`` — readiness probe.
* ``/metrics`` — Prometheus-format metrics for InfraMind itself.
* ``/incidents`` — list / detail of recent incidents.
* ``/alertmanager`` — POST webhook from Alertmanager.
"""
