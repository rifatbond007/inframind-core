"""Correlation: group anomalies into incidents.

Planned (Step 5):

* Sliding-window grouping by service + time.
* Fingerprint-based deduplication.
* Incident state machine: ``open -> updating -> resolved``.
* Reconciliation between 5-minute windows and 60s detection cycles.
"""
