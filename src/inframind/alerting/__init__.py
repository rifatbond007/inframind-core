"""Alerting: deliver deduplicated, actionable incident notifications.

Planned (Step 8):

* Routing rules (severity, service, time-of-day).
* Dedup window — only one alert per open incident.
* Payload builder — root cause + confidence + evidence + blast radius.
* Sinks — Telegram / Slack / email / webhook.
"""
