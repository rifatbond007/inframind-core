"""Public API surface (FastAPI). Only this package may import FastAPI.

Built so far:

* ``/healthz`` — liveness probe.
* ``POST /webhooks/alertmanager`` — the only push ingress (see ``alertmanager.py``).

Planned (Step 8): ``/readyz``, ``/metrics`` and the ``/incidents`` routes.
"""
