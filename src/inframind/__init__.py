"""InfraMind — Kubernetes multi-signal incident detection + graph-guided RCA.

Top-level modules (filled in by their owning phase; see docs/PROGRESS.md):

* :mod:`inframind.common`     — shared utils, signal schema, config.
* :mod:`inframind.ingestion`  — collectors (Prometheus, Loki, Jaeger/OTLP, Alertmanager).
* :mod:`inframind.detection`  — statistical detectors (Z-score, EWMA, error-rate spike).
* :mod:`inframind.correlation`— sliding-window grouping, dedup, incident state machine.
* :mod:`inframind.rca`        — NetworkX dependency graph, ranking, change-correlation.
* :mod:`inframind.llm`        — prompt templates, validator, evidence grounding.
* :mod:`inframind.alerting`   — routing, dedup, payload.
* :mod:`inframind.storage`    — PostgreSQL incident store, audit log.
* :mod:`inframind.api`        — FastAPI surface (/metrics, /healthz).
"""

__version__ = "0.0.0"
__all__ = ["__version__"]
