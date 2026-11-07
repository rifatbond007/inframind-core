"""Correlation: group anomalies into incidents and drive the baseline freeze.

P4 ships a minimal per-service ``IncidentTracker`` and the ``CorrelationWorker`` that runs the
"Detection + Correlation" stage. Windows, fingerprint deduplication and the full state machine
are P4b.
"""
