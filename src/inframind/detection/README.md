# `inframind.detection`

Statistical detectors that turn raw signals into anomalies (D3: no learning).

## Built (P4)

- `zscore.py` / `ewma.py` — Prometheus metrics, per `(service, metric)`, with noise floors and
  upward-only alerting on resource gauges.
- `error_spike.py` — Loki warning/error lines and Jaeger error spans, as a count spike.
- `latency_regression.py` — Jaeger p95 polls against a rolling median; the breach must persist.
- `freeze.py` — `BaselineFreezeCoordinator`: baselines freeze while an incident is open (D4);
  frozen detectors keep scoring.
- `engine.py` — `DetectionEngine.process(signal) -> list[Anomaly]`; `config.py` — parameters and
  `INFRAMIND_DETECTION_*` overrides; `base.py` — the `Detector` protocol and shared pieces.

Procedure and rationale: `docs/standards/05-add-detector.md`. Live gate: `make smoke-detection`.

## Owner

Moneem — ingestion, detection, correlation.
