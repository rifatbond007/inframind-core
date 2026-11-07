"""Detection: turn raw signals into anomalies (D3: statistical only, no learning).

* ``zscore`` / ``ewma`` — Prometheus metrics, per (service, metric), with noise floors.
* ``error_spike`` — Loki warning/error lines and Jaeger error spans, as a count spike.
* ``latency_regression`` — Jaeger p95 polls against a rolling median, persistence required.
* ``freeze`` — ``BaselineFreezeCoordinator``: baselines freeze while an incident is open (D4),
  but frozen detectors keep scoring, so a sustained fault keeps firing.
* ``engine`` — ``DetectionEngine.process(signal) -> list[Anomaly]``.

``Anomaly`` is defined in ``inframind.common.anomaly`` and re-exported from ``detection.anomaly``.
"""
