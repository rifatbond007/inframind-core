"""Detection: turn raw signals into anomalies.

Planned (Step 4):

* Metrics — Z-score, EWMA, percentile regression.
* Logs — error-rate spike, new-pattern detection.
* Traces — p95 latency regression, error-ratio regression.

**Important:** baselines must freeze while an incident is open (D4). Otherwise the
fault gets absorbed into the baseline and the detector stops firing.
"""
